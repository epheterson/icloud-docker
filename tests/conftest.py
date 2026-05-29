"""Pytest fixtures shared across the test tree.

Two autouse fixtures:

1. ``_restore_env_config_file_path`` — snapshot + restore
   ``ENV_CONFIG_FILE_PATH`` around every test so test teardowns that
   unset it don't bleed into later tests (the variable is set by the
   runner, not by tests).

2. ``_redirect_config_dir`` — point ``ICLOUD_DOCKER_CONFIG_DIR`` at a
   writable tempdir for the whole session. The container's ``/config``
   mount doesn't exist on dev hosts (macOS especially — read-only
   root). Without this redirect, ``src.usage.CACHE_FILE_NAME`` and
   ``src.DEFAULT_COOKIE_DIRECTORY`` (which both derive from
   ``ICLOUD_DOCKER_CONFIG_DIR``) point at ``/config/...`` paths that
   can't be created, and a swath of tests fail with FileNotFoundError.
"""

__author__ = "Mandar Patil (mandarons@pm.me)"

import os
import tempfile

import pytest

_ENV_KEY = "ENV_CONFIG_FILE_PATH"
_CONFIG_DIR_KEY = "ICLOUD_DOCKER_CONFIG_DIR"


@pytest.fixture(autouse=True)
def _restore_env_config_file_path():
    """Auto-applied around every test — snapshot + restore."""
    previous = os.environ.get(_ENV_KEY)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_ENV_KEY, None)
        else:
            os.environ[_ENV_KEY] = previous


@pytest.fixture(scope="session", autouse=True)
def _redirect_config_dir():
    """Session-wide ``ICLOUD_DOCKER_CONFIG_DIR`` → tempdir so the test
    suite can write usage cache + session_data on hosts where
    ``/config`` isn't writable (macOS dev hosts, CI sandboxes, etc).

    The redirect MUST be set before any ``from src import ...`` happens
    at module-import time (because ``DEFAULT_COOKIE_DIRECTORY`` is
    captured at import). Pytest collects conftest first, so this fires
    early — but we also import ``src`` here to force re-evaluation in
    case it was already imported by an earlier conftest.
    """
    if _CONFIG_DIR_KEY in os.environ:
        # Honor explicit caller override (e.g. CI integration tests).
        yield
        return

    tmpdir = tempfile.mkdtemp(prefix="icloud_test_config_")
    os.environ[_CONFIG_DIR_KEY] = tmpdir

    # Force re-evaluation of cached module-level constants that captured
    # the original "/config" path before we set the env var.
    import src
    import src.usage

    src.DEFAULT_COOKIE_DIRECTORY = os.path.join(tmpdir, "session_data")
    src.usage.CACHE_FILE_NAME = os.path.join(tmpdir, ".data")
    os.makedirs(src.DEFAULT_COOKIE_DIRECTORY, exist_ok=True)
    try:
        yield
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
        os.environ.pop(_CONFIG_DIR_KEY, None)
