"""Tests for the mount-marker failsafe (``photos.require_mount_marker`` /
``drive.require_mount_marker`` / ``app.mount_marker_filename``)."""

__author__ = "Mandar Patil (mandarons@pm.me)"

import logging
import os
import tempfile
import unittest

from src import config_parser, sync


class TestMountMarkerConfigHelpers(unittest.TestCase):
    """Defaults + read-through behaviour for the three config helpers."""

    def test_get_drive_require_mount_marker_default_false(self):
        """Default is False so existing installs see no behaviour change."""
        self.assertFalse(config_parser.get_drive_require_mount_marker(config={}))
        self.assertFalse(config_parser.get_drive_require_mount_marker(config={"drive": {}}))

    def test_get_drive_require_mount_marker_true_when_set(self):
        """Returns True when explicitly enabled."""
        self.assertTrue(
            config_parser.get_drive_require_mount_marker(config={"drive": {"require_mount_marker": True}}),
        )

    def test_get_photos_require_mount_marker_default_false(self):
        """Default is False so existing installs see no behaviour change."""
        self.assertFalse(config_parser.get_photos_require_mount_marker(config={}))
        self.assertFalse(config_parser.get_photos_require_mount_marker(config={"photos": {}}))

    def test_get_photos_require_mount_marker_true_when_set(self):
        """Returns True when explicitly enabled."""
        self.assertTrue(
            config_parser.get_photos_require_mount_marker(config={"photos": {"require_mount_marker": True}}),
        )

    def test_get_mount_marker_filename_default(self):
        """Default marker filename is ``.mounted`` (matches boredazfcuk convention)."""
        self.assertEqual(config_parser.get_mount_marker_filename(config={}), ".mounted")
        self.assertEqual(config_parser.get_mount_marker_filename(config={"app": {}}), ".mounted")

    def test_get_mount_marker_filename_when_configured(self):
        """Returns the configured filename when ``app.mount_marker_filename`` is set."""
        self.assertEqual(
            config_parser.get_mount_marker_filename(config={"app": {"mount_marker_filename": ".icloud-ok"}}),
            ".icloud-ok",
        )


class TestCheckMountMarker(unittest.TestCase):
    """Behaviour of ``sync._check_mount_marker``."""

    def setUp(self):
        """Create an isolated tempdir per test."""
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        """Remove the tempdir."""
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_true_when_not_required(self):
        """No marker file means no-op when require=False."""
        self.assertTrue(
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".mounted",
                required=False,
                service_name="Drive",
            ),
        )

    def test_returns_true_when_required_and_marker_present(self):
        """Marker file present satisfies the failsafe."""
        open(os.path.join(self.tmp, ".mounted"), "w").close()
        self.assertTrue(
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".mounted",
                required=True,
                service_name="Drive",
            ),
        )

    def test_returns_false_when_required_and_marker_absent(self):
        """Marker absent + required → False (caller should skip sync)."""
        self.assertFalse(
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".mounted",
                required=True,
                service_name="Drive",
            ),
        )

    def test_error_logged_when_marker_missing(self):
        """Refusal is logged at ERROR level with actionable instructions."""
        with self.assertLogs(sync.LOGGER, level=logging.ERROR) as cm:
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".mounted",
                required=True,
                service_name="Photos",
            )
        joined = "\n".join(cm.output)
        self.assertIn("Photos mount marker missing", joined)
        self.assertIn(os.path.join(self.tmp, ".mounted"), joined)
        self.assertIn("touch", joined)

    def test_custom_marker_filename_honoured(self):
        """``marker_filename`` parameter overrides the default."""
        open(os.path.join(self.tmp, ".icloud-ok"), "w").close()
        # Default name would fail; custom name succeeds.
        self.assertFalse(
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".mounted",
                required=True,
                service_name="Drive",
            ),
        )
        self.assertTrue(
            sync._check_mount_marker(
                destination_path=self.tmp,
                marker_filename=".icloud-ok",
                required=True,
                service_name="Drive",
            ),
        )


if __name__ == "__main__":
    unittest.main()
