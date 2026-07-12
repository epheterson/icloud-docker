"""Tests for Live Photo video extension handling + migration.

Covers the fix (paired videos get a .MOV extension, not the still's .HEIC), the
one-shot migration that renames pre-existing mislabeled files, and the config
option that gates it.
"""

import os
import types

from src import config_parser, live_photo_migration
from src.photo_path_utils import (
    generate_photo_filename_with_metadata,
    get_photo_name_and_extension,
)

# ftyp box headers (size + 'ftyp' + major brand) for crafting test fixtures.
_FTYP_QUICKTIME = b"\x00\x00\x00\x18ftypqt  \x00\x00\x02\x00qt  "
_FTYP_HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"


def _photo(filename, versions):
    """Minimal stand-in for an iCloudPy PhotoAsset."""
    return types.SimpleNamespace(filename=filename, versions=versions, id="ASSET-ID-1")


class TestLivePhotoExtension:
    def test_live_video_original_maps_to_mov(self):
        photo = _photo(
            "IMG_1234.HEIC",
            {
                "original": {"type": "public.heic"},
                "live_video_original": {"type": "com.apple.quicktime-movie"},
            },
        )
        name, ext = get_photo_name_and_extension(photo, "live_video_original")
        assert name == "IMG_1234"
        assert ext == "MOV"

    def test_live_video_unknown_type_defaults_to_mov(self):
        photo = _photo("IMG_1.HEIC", {"live_video_original": {"type": None}})
        _name, ext = get_photo_name_and_extension(photo, "live_video_original")
        assert ext == "MOV"

    def test_live_video_medium_and_thumb_are_mov(self):
        photo = _photo(
            "IMG_2.JPG",
            {
                "live_video_medium": {"type": "com.apple.quicktime-movie"},
                "live_video_thumb": {"type": "com.apple.quicktime-movie"},
            },
        )
        assert get_photo_name_and_extension(photo, "live_video_medium")[1] == "MOV"
        assert get_photo_name_and_extension(photo, "live_video_thumb")[1] == "MOV"

    def test_still_original_keeps_its_extension(self):
        photo = _photo("IMG_1234.HEIC", {"original": {"type": "public.heic"}})
        _name, ext = get_photo_name_and_extension(photo, "original")
        assert ext == "HEIC"

    def test_filename_with_metadata_ends_in_mov(self):
        photo = _photo(
            "IMG_9.HEIC",
            {"live_video_original": {"type": "com.apple.quicktime-movie"}},
        )
        filename = generate_photo_filename_with_metadata(photo, "live_video_original")
        assert filename.endswith(".MOV")
        assert "__live_video_original__" in filename


class TestQuickTimeDetection:
    def test_quicktime_brand_detected(self, tmp_path):
        f = tmp_path / "v.bin"
        f.write_bytes(_FTYP_QUICKTIME)
        assert live_photo_migration.is_quicktime_movie(str(f)) is True

    def test_heic_image_not_detected(self, tmp_path):
        f = tmp_path / "i.bin"
        f.write_bytes(_FTYP_HEIC)
        assert live_photo_migration.is_quicktime_movie(str(f)) is False

    def test_non_ftyp_not_detected(self, tmp_path):
        f = tmp_path / "j.bin"
        f.write_bytes(b"\xff\xd8\xff\xe0JFIF")
        assert live_photo_migration.is_quicktime_movie(str(f)) is False

    def test_unreadable_path_not_detected(self, tmp_path):
        missing = tmp_path / "does-not-exist.bin"
        assert live_photo_migration.is_quicktime_movie(str(missing)) is False


class TestMigration:
    def _make_live_video(self, d, name, payload=_FTYP_QUICKTIME):
        p = os.path.join(d, name)
        with open(p, "wb") as handle:
            handle.write(payload)
        return p

    def test_dry_run_reports_but_does_not_rename(self, tmp_path):
        src = self._make_live_video(
            str(tmp_path),
            "IMG_1__live_video_original__abc.HEIC",
        )
        stats = live_photo_migration.run_migration([str(tmp_path)], "dry-run")
        assert stats["renamed"] == 1
        assert os.path.exists(src)  # untouched
        assert not os.path.exists(src[:-5] + ".MOV")

    def test_apply_renames_to_mov(self, tmp_path):
        src = self._make_live_video(
            str(tmp_path),
            "IMG_1__live_video_original__abc.HEIC",
        )
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["renamed"] == 1
        assert not os.path.exists(src)
        assert os.path.exists(
            os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MOV"),
        )

    def test_apply_skips_when_target_exists(self, tmp_path):
        self._make_live_video(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        # target already present
        open(
            os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MOV"),
            "wb",
        ).close()
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["skipped_exists"] == 1
        assert stats["renamed"] == 0

    def test_apply_skips_real_image(self, tmp_path):
        # A file that matches the name pattern but is genuinely a HEIC image
        # must NOT be renamed (safety belt against a naming coincidence).
        src = self._make_live_video(
            str(tmp_path),
            "IMG_1__live_video_original__abc.HEIC",
            payload=_FTYP_HEIC,
        )
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["skipped_not_video"] == 1
        assert stats["renamed"] == 0
        assert os.path.exists(src)

    def test_ignores_unrelated_files(self, tmp_path):
        self._make_live_video(str(tmp_path), "IMG_1__original__abc.HEIC")  # a still
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["scanned"] == 0

    def test_off_mode_is_noop(self, tmp_path):
        src = self._make_live_video(
            str(tmp_path),
            "IMG_1__live_video_original__abc.HEIC",
        )
        stats = live_photo_migration.run_migration([str(tmp_path)], "off")
        assert stats == {
            "scanned": 0,
            "renamed": 0,
            "skipped_exists": 0,
            "skipped_not_video": 0,
        }
        assert os.path.exists(src)

    def test_migrate_directory_off_mode_returns_empty(self, tmp_path):
        # Direct call with a non-active mode short-circuits before walking.
        stats = live_photo_migration.migrate_directory(str(tmp_path), "off")
        assert stats["renamed"] == 0

    def test_missing_destination_is_handled(self):
        stats = live_photo_migration.run_migration(["/no/such/dir/xyz"], "apply")
        assert stats == {
            "scanned": 0,
            "renamed": 0,
            "skipped_exists": 0,
            "skipped_not_video": 0,
        }


class TestSyncStartupHook:
    def test_apply_mode_runs_migration(self, tmp_path):
        from src import sync

        dest = tmp_path / "photos"
        dest.mkdir()
        (dest / "IMG_1__live_video_original__x.HEIC").write_bytes(_FTYP_QUICKTIME)
        config = {
            "photos": {
                "destination": str(dest),
                "migrate_mislabeled_live_videos": "apply",
            },
        }
        sync._run_live_photo_migration_if_configured(config)
        assert (dest / "IMG_1__live_video_original__x.MOV").exists()

    def test_off_and_missing_photos_are_noops(self):
        from src import sync

        # None config, config without photos, and default (off) all short-circuit.
        sync._run_live_photo_migration_if_configured(None)
        sync._run_live_photo_migration_if_configured({})
        sync._run_live_photo_migration_if_configured({"photos": {}})


class TestMigrationConfig:
    def test_defaults_off(self):
        assert config_parser.get_photos_migrate_mislabeled_live_videos({"photos": {}}) == "off"

    def test_reads_apply(self):
        cfg = {"photos": {"migrate_mislabeled_live_videos": "apply"}}
        assert config_parser.get_photos_migrate_mislabeled_live_videos(cfg) == "apply"

    def test_reads_dry_run_case_insensitive(self):
        cfg = {"photos": {"migrate_mislabeled_live_videos": "Dry-Run"}}
        assert config_parser.get_photos_migrate_mislabeled_live_videos(cfg) == "dry-run"

    def test_invalid_falls_back_to_off(self):
        cfg = {"photos": {"migrate_mislabeled_live_videos": "yolo"}}
        assert config_parser.get_photos_migrate_mislabeled_live_videos(cfg) == "off"
