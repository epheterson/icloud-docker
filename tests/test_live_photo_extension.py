"""Tests for Live Photo video extension handling + migration.

Covers the fix (paired videos get a .MOV/.MP4 extension, not the still's .HEIC),
the download-path self-heal (existing .HEIC videos are renamed in place instead
of re-downloaded), the one-shot migration, and the config option that gates it.
"""

import os
import types

from src import config_parser, live_photo_migration
from src.photo_download_manager import generate_photo_path
from src.photo_path_utils import (
    generate_photo_filename_with_metadata,
    get_photo_name_and_extension,
)

# ftyp box headers (size + 'ftyp' + major brand [+ compatible brands]).
_FTYP_QUICKTIME = b"\x00\x00\x00\x18ftypqt  \x00\x00\x02\x00qt  "
_FTYP_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
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
        assert get_photo_name_and_extension(photo, "live_video_original")[1] == "MOV"

    def test_live_video_mp4_type_maps_to_mp4(self):
        photo = _photo("IMG_1.HEIC", {"live_video_original": {"type": "public.mpeg-4"}})
        assert get_photo_name_and_extension(photo, "live_video_original")[1] == "MP4"

    def test_still_original_keeps_its_extension(self):
        photo = _photo("IMG_1234.HEIC", {"original": {"type": "public.heic"}})
        assert get_photo_name_and_extension(photo, "original")[1] == "HEIC"

    def test_filename_with_metadata_ends_in_mov(self):
        photo = _photo("IMG_9.HEIC", {"live_video_original": {"type": "com.apple.quicktime-movie"}})
        filename = generate_photo_filename_with_metadata(photo, "live_video_original")
        assert filename.endswith(".MOV")
        assert "__live_video_original__" in filename


class TestClassifyLiveVideo:
    def test_quicktime_is_mov(self, tmp_path):
        f = tmp_path / "v.bin"
        f.write_bytes(_FTYP_QUICKTIME)
        assert live_photo_migration.classify_live_video(str(f)) == "MOV"

    def test_mp4_is_mp4(self, tmp_path):
        f = tmp_path / "v.bin"
        f.write_bytes(_FTYP_MP4)
        assert live_photo_migration.classify_live_video(str(f)) == "MP4"

    def test_heic_image_is_none(self, tmp_path):
        f = tmp_path / "i.bin"
        f.write_bytes(_FTYP_HEIC)
        assert live_photo_migration.classify_live_video(str(f)) is None

    def test_non_ftyp_is_none(self, tmp_path):
        f = tmp_path / "j.bin"
        f.write_bytes(b"\xff\xd8\xff\xe0JFIF")
        assert live_photo_migration.classify_live_video(str(f)) is None

    def test_unreadable_is_none(self, tmp_path):
        assert live_photo_migration.classify_live_video(str(tmp_path / "nope.bin")) is None


class TestMigration:
    def _make(self, d, name, payload=_FTYP_QUICKTIME):
        p = os.path.join(d, name)
        with open(p, "wb") as handle:
            handle.write(payload)
        return p

    def test_dry_run_reports_but_does_not_rename(self, tmp_path):
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        stats = live_photo_migration.run_migration([str(tmp_path)], "dry-run")
        assert stats["renamed"] == 1
        assert os.path.exists(src)
        assert not os.path.exists(src[:-5] + ".MOV")

    def test_apply_renames_to_mov(self, tmp_path):
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["renamed"] == 1
        assert not os.path.exists(src)
        assert os.path.exists(os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MOV"))

    def test_apply_renames_mp4_container_to_mp4(self, tmp_path):
        self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC", payload=_FTYP_MP4)
        live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert os.path.exists(os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MP4"))

    def test_apply_removes_orphan_when_correct_exists(self, tmp_path):
        # A correct .MOV already exists (e.g. re-downloaded); the mislabeled
        # .HEIC duplicate must be removed, not reported as already-correct.
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        open(os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MOV"), "wb").close()
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["removed_orphans"] == 1
        assert stats["renamed"] == 0
        assert not os.path.exists(src)

    def test_dry_run_reports_orphan_without_removing(self, tmp_path):
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        open(os.path.join(str(tmp_path), "IMG_1__live_video_original__abc.MOV"), "wb").close()
        stats = live_photo_migration.run_migration([str(tmp_path)], "dry-run")
        assert stats["removed_orphans"] == 1
        assert os.path.exists(src)

    def test_apply_skips_real_image(self, tmp_path):
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC", payload=_FTYP_HEIC)
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["skipped_not_video"] == 1
        assert stats["renamed"] == 0
        assert os.path.exists(src)

    def test_ignores_unrelated_files(self, tmp_path):
        self._make(str(tmp_path), "IMG_1__original__abc.HEIC")  # a still
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["scanned"] == 0

    def test_off_mode_is_noop(self, tmp_path):
        src = self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        stats = live_photo_migration.run_migration([str(tmp_path)], "off")
        assert stats == live_photo_migration._empty_stats()
        assert os.path.exists(src)

    def test_apply_writes_sentinel_and_second_run_skips_walk(self, tmp_path):
        self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert os.path.exists(os.path.join(str(tmp_path), live_photo_migration._SENTINEL))
        # a new mislabeled file added after the sentinel is NOT scanned again
        self._make(str(tmp_path), "IMG_2__live_video_original__def.HEIC")
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["scanned"] == 0

    def test_sentinel_write_failure_is_handled(self, tmp_path, monkeypatch):
        # If the sentinel can't be written, apply must still complete (renames
        # already happened) without raising.
        self._make(str(tmp_path), "IMG_1__live_video_original__abc.HEIC")
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file).endswith(live_photo_migration._SENTINEL):
                msg = "sentinel unwritable"
                raise OSError(msg)
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(live_photo_migration, "open", fake_open, raising=False)
        stats = live_photo_migration.run_migration([str(tmp_path)], "apply")
        assert stats["renamed"] == 1

    def test_migrate_directory_off_mode_returns_empty(self, tmp_path):
        assert live_photo_migration.migrate_directory(str(tmp_path), "off")["renamed"] == 0

    def test_missing_destination_is_handled(self):
        assert live_photo_migration.run_migration(["/no/such/dir/xyz"], "apply") == (
            live_photo_migration._empty_stats()
        )


class TestDownloadSelfHeal:
    def test_generate_photo_path_renames_legacy_heic_video(self, tmp_path):
        # An existing IMG__live_video_original__<id>.HEIC (from the buggy version)
        # must be renamed in place to the corrected .MOV, not left for re-download.
        photo = _photo("IMG_1.HEIC", {"live_video_original": {"type": "com.apple.quicktime-movie"}})
        corrected = generate_photo_filename_with_metadata(photo, "live_video_original")
        assert corrected.endswith(".MOV")
        legacy = os.path.join(str(tmp_path), corrected[: -len(".MOV")] + ".HEIC")
        with open(legacy, "wb") as handle:
            handle.write(_FTYP_QUICKTIME)

        result = generate_photo_path(photo, "live_video_original", str(tmp_path), None)

        assert result == os.path.join(str(tmp_path), corrected)
        assert os.path.exists(result)  # renamed in place
        assert not os.path.exists(legacy)  # old .HEIC gone (no re-download, no orphan)

    def test_generate_photo_path_no_legacy_is_noop(self, tmp_path):
        photo = _photo("IMG_2.HEIC", {"live_video_original": {"type": "com.apple.quicktime-movie"}})
        result = generate_photo_path(photo, "live_video_original", str(tmp_path), None)
        assert result.endswith(".MOV")
        assert not os.path.exists(result)  # nothing pre-existing to rename


class TestSyncStartupHook:
    def test_apply_mode_runs_against_real_resolved_path(self, tmp_path):
        from src import sync

        # Real config shape: app.root + relative photos.destination. The hook must
        # resolve the SAME absolute dir the sync writes to (regression for the
        # get_ vs prepare_ path bug), not the raw relative 'photos'.
        root = tmp_path / "icloud"
        photos = root / "photos"
        photos.mkdir(parents=True)
        (photos / "IMG_1__live_video_original__x.HEIC").write_bytes(_FTYP_QUICKTIME)
        config = {
            "app": {"root": str(root)},
            "photos": {
                "destination": "photos",
                "migrate_mislabeled_live_videos": "apply",
            },
        }
        sync._run_live_photo_migration_if_configured(config)
        assert (photos / "IMG_1__live_video_original__x.MOV").exists()

    def test_off_and_missing_photos_are_noops(self):
        from src import sync

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
