"""Tests for the optional ``photos.filename_format`` config knob.

Added 2026-05-27. Lets users migrating from boredazfcuk/docker-icloudpd
(which uses plain ``IMG_1234.HEIC`` naming) point this container at their
existing photo tree without triggering a full re-download.
"""

import unittest
from unittest.mock import MagicMock

from src import config_parser
from src.photo_path_utils import (
    _DEFAULT_FILENAME_FORMAT,  # noqa: F401  (re-imported below to assert default)
    generate_photo_filename_with_metadata,
    set_default_filename_format,
)


def _fake_photo(filename="IMG_1234.HEIC", photo_id="some-cloudkit-id"):
    photo = MagicMock()
    photo.filename = filename
    photo.id = photo_id
    photo.versions = {"original": {"type": "public.heic"}}
    return photo


class TestGetPhotosFilenameFormat(unittest.TestCase):
    def test_default_is_metadata(self):
        assert config_parser.get_photos_filename_format({}) == "metadata"

    def test_simple_is_accepted(self):
        config = {"photos": {"filename_format": "simple"}}
        assert config_parser.get_photos_filename_format(config) == "simple"

    def test_uppercase_is_normalised(self):
        config = {"photos": {"filename_format": "SIMPLE"}}
        assert config_parser.get_photos_filename_format(config) == "simple"

    def test_unknown_value_falls_back_to_metadata(self):
        config = {"photos": {"filename_format": "weird-thing"}}
        assert config_parser.get_photos_filename_format(config) == "metadata"


class TestGeneratePhotoFilename(unittest.TestCase):
    def setUp(self):
        # Reset module-level default before each test so leakage between
        # tests doesn't matter.
        set_default_filename_format("metadata")

    def test_metadata_format_produces_suffixed_name(self):
        photo = _fake_photo("IMG_1234.HEIC", "abc")
        name = generate_photo_filename_with_metadata(photo, "original", "metadata")
        # ``name__filesize__base64id.ext``
        assert name.startswith("IMG_1234__original__")
        assert name.endswith(".HEIC")

    def test_simple_format_produces_plain_name(self):
        photo = _fake_photo("IMG_1234.HEIC", "abc")
        name = generate_photo_filename_with_metadata(photo, "original", "simple")
        assert name == "IMG_1234.HEIC"

    def test_simple_format_handles_extensionless_filename(self):
        photo = _fake_photo("noextension", "abc")
        name = generate_photo_filename_with_metadata(photo, "original", "simple")
        assert name == "noextension"

    def test_explicit_none_uses_module_default(self):
        photo = _fake_photo("IMG_1234.HEIC", "abc")
        set_default_filename_format("simple")
        name = generate_photo_filename_with_metadata(photo, "original", None)
        assert name == "IMG_1234.HEIC"

    def test_no_argument_uses_module_default(self):
        photo = _fake_photo("IMG_1234.HEIC", "abc")
        set_default_filename_format("simple")
        name = generate_photo_filename_with_metadata(photo, "original")
        assert name == "IMG_1234.HEIC"

    def test_default_format_remains_metadata_for_backward_compat(self):
        photo = _fake_photo("IMG_1234.HEIC", "abc")
        # Module-level default not changed → metadata-suffix format
        name = generate_photo_filename_with_metadata(photo, "original")
        assert "__original__" in name


class TestSetDefaultFilenameFormat(unittest.TestCase):
    def setUp(self):
        set_default_filename_format("metadata")

    def test_set_to_simple(self):
        set_default_filename_format("simple")
        from src import photo_path_utils

        assert photo_path_utils._DEFAULT_FILENAME_FORMAT == "simple"

    def test_set_to_metadata(self):
        set_default_filename_format("simple")
        set_default_filename_format("metadata")
        from src import photo_path_utils

        assert photo_path_utils._DEFAULT_FILENAME_FORMAT == "metadata"

    def test_unknown_value_is_ignored(self):
        set_default_filename_format("simple")  # baseline
        set_default_filename_format("invalid")
        from src import photo_path_utils

        # Stays at simple — invalid value silently rejected
        assert photo_path_utils._DEFAULT_FILENAME_FORMAT == "simple"
