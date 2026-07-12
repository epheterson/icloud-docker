"""Live Photo video-extension migration.

Earlier releases downloaded the Live Photo paired video (``live_video_original``
/ ``live_video_medium`` / ``live_video_thumb``) but wrote it with the still's
extension, e.g. ``IMG_1234__live_video_original__<id>.HEIC``. That file is a
QuickTime (or MP4) movie, so every downstream image tool rejects it as an
unsupported image and no thumbnail is produced.

This module finds those mislabeled files and renames them to the correct video
extension so they become first-class videos (and photo managers can pair them
back to the still). It only ever touches a file it has positively confirmed is
an ISO-BMFF movie and NOT a real HEIF/AVIF image, so a genuine image that
happens to match the name pattern is left untouched.

To keep it cheap on very large libraries, an ``apply`` run drops a sentinel in
each destination when it finishes, so subsequent restarts do not re-walk the
whole tree.
"""

___author___ = "Mandar Patil <mandarons@pm.me>"

import os

from src import get_logger
from src.photo_path_utils import _LIVE_VIDEO_SIZES

LOGGER = get_logger()

# Migration modes (config: photos.migrate_mislabeled_live_videos)
MODE_OFF = "off"
MODE_DRY_RUN = "dry-run"
MODE_APPLY = "apply"

# The version labels our filenames embed for the paired video (name__size__id.ext),
# derived from the single source of truth in photo_path_utils.
_LIVE_VIDEO_LABELS = tuple(f"__{size}__" for size in sorted(_LIVE_VIDEO_SIZES))

# Extensions a mislabeled live video may currently carry (the still's extension).
_MISLABELED_EXTS = (".heic", ".heif", ".jpg", ".jpeg")

# Dropped in each destination once an apply run finishes, so later restarts skip
# the full-tree walk.
_SENTINEL = ".icloud_live_photo_migration.done"

# ISO-BMFF ``ftyp`` brands that mean "this really is a HEIF/AVIF image" and must
# never be renamed. Anything else with an ftyp box is treated as a movie (we
# already matched the __live_video_*__ name, so the payload is the paired video).
_IMAGE_BRANDS = frozenset(
    {
        "heic",
        "heix",
        "heim",
        "heis",
        "hevc",
        "hevx",
        "hevm",
        "hevs",
        "mif1",
        "msf1",
        "avif",
        "avis",
    },
)
# Major brands that specifically map to an .MP4 container; everything else with
# an ftyp box is treated as QuickTime (the Live Photo default).
_MP4_MAJOR_BRANDS = frozenset({"mp41", "mp42", "isom", "iso2", "m4v ", "avc1", "3gp4", "3gp5"})

# Top-level QuickTime atoms. Many iOS Live Photo movies carry NO ftyp box and
# start straight with one of these (commonly ``wide`` + ``mdat``), so an
# ftyp-only check would miss them.
_QUICKTIME_ATOMS = frozenset({"moov", "mdat", "wide", "free", "skip", "pnot"})


def classify_live_video(path: str) -> str | None:
    """Return the correct video extension for a mislabeled live-video file.

    Returns ``"MOV"`` or ``"MP4"`` for a movie, or None if the file is a genuine
    HEIF/AVIF image (or not a recognized movie at all), in which case it must be
    left alone. Reads the first top-level box: an ``ftyp`` box's brands
    distinguish image vs MP4 vs QuickTime; a bare QuickTime atom (``wide`` /
    ``mdat`` / ``moov`` ...) means a ftyp-less QuickTime movie.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if len(head) < 8:
        return None
    atom = head[4:8].decode("latin-1")
    if atom == "ftyp":
        brands = [head[8:12].decode("latin-1")]
        brands.extend(head[off : off + 4].decode("latin-1") for off in range(16, len(head) - 3, 4))
        if any(b in _IMAGE_BRANDS for b in brands):
            return None
        return "MP4" if brands[0] in _MP4_MAJOR_BRANDS else "MOV"
    if atom in _QUICKTIME_ATOMS:
        return "MOV"
    return None


def _looks_like_mislabeled_live_video(filename: str) -> bool:
    """True if the name is one of our live-video files carrying an image extension."""
    lower = filename.lower()
    if not any(label in lower for label in _LIVE_VIDEO_LABELS):
        return False
    return lower.endswith(_MISLABELED_EXTS)


def _empty_stats() -> dict:
    return {"scanned": 0, "renamed": 0, "removed_orphans": 0, "skipped_not_video": 0}


def migrate_directory(destination: str, mode: str) -> dict:
    """Walk ``destination`` and correct mislabeled live-photo videos.

    Renames each confirmed mislabeled video to its real extension. If the correct
    video already exists (e.g. a prior re-download), the mislabeled duplicate is
    a redundant orphan and is removed in apply mode. Genuine images are skipped.

    Args:
        destination: Root directory to scan recursively.
        mode: ``dry-run`` (report only) or ``apply`` (rename/remove).

    Returns:
        Stats dict: scanned, renamed, removed_orphans, skipped_not_video.
    """
    stats = _empty_stats()
    if mode not in (MODE_DRY_RUN, MODE_APPLY):
        return stats
    if not destination or not os.path.isdir(destination):
        LOGGER.warning(f"Live-photo migration: destination not found: {destination}")
        return stats

    sentinel = os.path.join(destination, _SENTINEL)
    if mode == MODE_APPLY and os.path.exists(sentinel):
        LOGGER.info(f"Live Photo migration already applied in {destination}; skipping scan.")
        return stats

    for dirpath, _dirs, files in os.walk(destination):
        for filename in files:
            if not _looks_like_mislabeled_live_video(filename):
                continue
            stats["scanned"] += 1
            source = os.path.join(dirpath, filename)

            extension = classify_live_video(source)
            if extension is None:
                stats["skipped_not_video"] += 1
                LOGGER.warning(f"Live-photo migration: not a movie, leaving as-is: {source}")
                continue

            root, _ext = os.path.splitext(source)
            target = f"{root}.{extension}"

            if os.path.exists(target):
                # The correct video is already present, so this mislabeled file
                # is a redundant duplicate of the same asset+version. Remove it.
                stats["removed_orphans"] += 1
                if mode == MODE_APPLY:
                    os.remove(source)
                    LOGGER.info(f"Removed orphaned mislabeled live video {source} (have {target})")
                else:
                    LOGGER.info(f"[dry-run] would remove orphan {source} (have {target})")
                continue

            stats["renamed"] += 1
            if mode == MODE_APPLY:
                os.rename(source, target)
                LOGGER.info(f"Renamed mislabeled live video {source} -> {target}")
            else:
                LOGGER.info(f"[dry-run] would rename {source} -> {target}")

    if mode == MODE_APPLY:
        try:
            with open(sentinel, "w", encoding="utf-8") as handle:
                handle.write("done\n")
        except OSError as e:
            LOGGER.warning(f"Live-photo migration: could not write sentinel {sentinel}: {e}")

    return stats


def run_migration(destinations: list[str], mode: str) -> dict:
    """Run the migration across one or more destination roots.

    Args:
        destinations: Photo destination directories to scan.
        mode: ``off`` (no-op), ``dry-run`` (report), or ``apply`` (rename/remove).

    Returns:
        Aggregated stats dict.
    """
    totals = _empty_stats()
    if mode not in (MODE_DRY_RUN, MODE_APPLY):
        return totals

    verb = "Reporting" if mode == MODE_DRY_RUN else "Applying"
    LOGGER.info(f"{verb} Live Photo video-extension migration ...")
    for destination in destinations:
        result = migrate_directory(destination, mode)
        for key in totals:
            totals[key] += result[key]

    LOGGER.info(
        f"Live Photo migration {mode}: {totals['renamed']} renamed, "
        f"{totals['removed_orphans']} orphans removed, {totals['skipped_not_video']} not-a-movie "
        f"(of {totals['scanned']} candidates)",
    )
    return totals
