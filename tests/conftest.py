"""Pytest fixtures shared across the test tree.

Currently a single autouse fixture that snapshots + restores the
``ENV_CONFIG_FILE_PATH`` environment variable around every test.

Why this exists
---------------
Several existing test files unset ``ENV_CONFIG_FILE_PATH`` in their
``tearDown`` (a well-intentioned "clean up after myself" pattern that
predates pytest). The problem: the variable was set by the *test runner
invocation*, not by the test, and clearing it bleeds into later tests
that then fall back to ``DEFAULT_CONFIG_FILE_PATH`` (= the production
``config.yaml`` at the repo root). That production config has
``root: /icloud`` — an absolute container path — and any test that
walks ``prepare_root_destination`` on it tries to ``mkdir /icloud`` on
the developer's host, which fails on macOS (read-only root) and is
generally undesirable on Linux too.

This fixture snapshots the variable at the start of every test and
restores it at teardown, regardless of what the test does to it.
"""

__author__ = "Mandar Patil (mandarons@pm.me)"

import os

import pytest

_ENV_KEY = "ENV_CONFIG_FILE_PATH"


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
