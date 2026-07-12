"""Live Photo video-extension migration.

Earlier releases downloaded the Live Photo paired video (``live_video_original``
/ ``live_video_medium`` / ``live_video_thumb``) but wrote it with the still's
extension, e.g. ``IMG_1234__live_video_original__<id>.HEIC``. That file is a
QuickTime movie, so every downstream image tool rejects it as an unsupported
image and no thumbnail is produced.

This module finds those mislabeled files and renames them to ``.MOV`` so they
become first-class videos (and photo managers can pair them back to the still).
It is idempotent and, in ``apply`` mode, only touches files it has positively
confirmed are QuickTime movies via their ``ftyp`` box — never a real image.
"""

___author___ = "Mandar Patil <mandarons@pm.me>"

import os

from src import get_logger

LOGGER = get_logger()

# Migration modes (config: photos.migrate_mislabeled_live_videos)
MODE_OFF = "off"
MODE_DRY_RUN = "dry-run"
MODE_APPLY = "apply"
VALID_MODES = frozenset({MODE_OFF, MODE_DRY_RUN, MODE_APPLY})

# The version labels our filenames embed for the paired video (name__size__id.ext).
_LIVE_VIDEO_LABELS = (
    "__live_video_original__",
    "__live_video_medium__",
    "__live_video_thumb__",
)

# Extensions a mislabeled live video may currently carry (the still's extension).
_MISLABELED_EXTS = (".heic", ".heif", ".jpg", ".jpeg")

# ISO-BMFF ``ftyp`` brands that mean "this is a movie, not an image".
_MOVIE_BRANDS = frozenset(
    {"qt  ", "mp41", "mp42", "isom", "iso2", "m4v ", "avc1", "3gp5", "3gp4"},
)
# ...and brands that mean "this really is a HEIF image" (never rename these).
_IMAGE_BRANDS = frozenset(
    {"heic", "heix", "heim", "heis", "hevc", "hevx", "mif1", "msf1", "avif"},
)


def _read_ftyp_brands(path: str) -> list[str] | None:
    """Return the major + compatible brands from a file's ``ftyp`` box, or None.

    Reads only the leading 64 bytes: enough for the major brand and a dozen
    compatible brands. Returns None for anything that is not ftyp-led.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brands = [head[8:12].decode("latin-1")]
    brands.extend(
        head[off : off + 4].decode("latin-1") for off in range(16, len(head) - 3, 4)
    )
    return brands


def is_quicktime_movie(path: str) -> bool:
    """True if the file's ``ftyp`` box identifies a movie and not a HEIF image."""
    brands = _read_ftyp_brands(path)
    if brands is None:
        return False
    if any(b in _IMAGE_BRANDS for b in brands):
        return False
    return any(b in _MOVIE_BRANDS for b in brands)


def _looks_like_mislabeled_live_video(filename: str) -> bool:
    """True if the name is one of our live-video files carrying an image extension."""
    lower = filename.lower()
    if not any(label in lower for label in _LIVE_VIDEO_LABELS):
        return False
    return lower.endswith(_MISLABELED_EXTS)


def _target_path(path: str) -> str:
    """The corrected ``.MOV`` path for a mislabeled live-video file."""
    root, _ext = os.path.splitext(path)
    return root + ".MOV"


def migrate_directory(destination: str, mode: str) -> dict:
    """Walk ``destination`` and rename mislabeled live-photo videos to ``.MOV``.

    Args:
        destination: Root directory to scan recursively.
        mode: One of ``dry-run`` (report only) or ``apply`` (rename).

    Returns:
        Dict with counts: scanned, renamed, skipped_exists, skipped_not_video.
    """
    stats = {"scanned": 0, "renamed": 0, "skipped_exists": 0, "skipped_not_video": 0}
    if mode not in (MODE_DRY_RUN, MODE_APPLY):
        return stats
    if not destination or not os.path.isdir(destination):
        LOGGER.warning(f"Live-photo migration: destination not found: {destination}")
        return stats

    for dirpath, _dirs, files in os.walk(destination):
        for filename in files:
            if not _looks_like_mislabeled_live_video(filename):
                continue
            stats["scanned"] += 1
            source = os.path.join(dirpath, filename)
            target = _target_path(source)

            if os.path.exists(target):
                stats["skipped_exists"] += 1
                continue
            # Safety belt: only rename files we can positively confirm are movies.
            if not is_quicktime_movie(source):
                stats["skipped_not_video"] += 1
                LOGGER.warning(
                    f"Live-photo migration: not a movie, leaving as-is: {source}",
                )
                continue

            if mode == MODE_DRY_RUN:
                LOGGER.info(f"[dry-run] would rename {source} -> {target}")
                stats["renamed"] += 1
            else:
                os.rename(source, target)
                LOGGER.info(f"Renamed mislabeled live video {source} -> {target}")
                stats["renamed"] += 1

    return stats


def run_migration(destinations: list[str], mode: str) -> dict:
    """Run the migration across one or more destination roots.

    Args:
        destinations: Photo destination directories to scan.
        mode: ``off`` (no-op), ``dry-run`` (report), or ``apply`` (rename).

    Returns:
        Aggregated stats dict.
    """
    totals = {"scanned": 0, "renamed": 0, "skipped_exists": 0, "skipped_not_video": 0}
    if mode not in (MODE_DRY_RUN, MODE_APPLY):
        return totals

    verb = "Reporting" if mode == MODE_DRY_RUN else "Applying"
    LOGGER.info(f"{verb} Live Photo video-extension migration (.HEIC -> .MOV) ...")
    for destination in destinations:
        result = migrate_directory(destination, mode)
        for key in totals:
            totals[key] += result[key]

    LOGGER.info(
        f"Live Photo migration {mode}: {totals['renamed']} renamed, "
        f"{totals['skipped_exists']} already correct, {totals['skipped_not_video']} not-a-movie "
        f"(of {totals['scanned']} candidates)",
    )
    return totals
