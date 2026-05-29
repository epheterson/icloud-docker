"""Main module."""

__author__ = "Mandar Patil (mandarons@pm.me)"

import argparse
import os

from src import (
    DEFAULT_CONFIG_FILE_PATH,
    ENV_CONFIG_FILE_PATH_KEY,
    config_parser,
    read_config,
    sync,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icloud-docker",
        description="iCloud Drive + Photos backup loop. See config.yaml for runtime settings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Authenticate, summarise what would be synced, then exit "
            "without downloading or modifying any files. Useful for "
            "verifying credentials + mount paths + config before the "
            "real sync loop is allowed to run."
        ),
    )
    parser.add_argument(
        "--check-files",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Only meaningful with --dry-run. Walks N photos per library "
            "AND N Drive files, reporting per-service counts of "
            "would_skip / size_mismatch / not_found / error against your "
            "on-disk tree. Use this BEFORE a real sync to confirm a "
            "boredazfcuk → mandarons (or any cross-tool) migration will "
            "recognise existing files instead of re-downloading them. "
            "Pass 0 to walk every photo + every Drive file (slow on "
            "large libraries — recommend 50–200 first)."
        ),
    )
    return parser


def run(argv: list[str] | None = None) -> None:
    """Process entry point.

    Reads config, optionally starts the web UI thread (when
    ``app.web_ui.enabled`` is true), then hands off to ``sync.sync``.
    Extracted from ``__main__`` so the test suite can call it directly
    with mocks for ``src.web`` and ``src.sync``.
    """
    parser = _build_arg_parser()
    # parse_known_args so tests calling ``main.run()`` under pytest don't
    # blow up on pytest's own argv.
    args, _ = parser.parse_known_args(argv)

    # The web-UI check is best-effort — if read_config can't parse the
    # file (partial / malformed config), skip the web thread and let
    # sync.sync() handle the malformed-config retry path itself.
    try:
        config_path = os.environ.get(ENV_CONFIG_FILE_PATH_KEY, DEFAULT_CONFIG_FILE_PATH)
        config = read_config(config_path=config_path)
        web_ui_enabled = config_parser.get_web_ui_enabled(config=config)
    except Exception:
        config = None
        web_ui_enabled = False

    if web_ui_enabled and config is not None:
        from src import web

        web.start_in_thread(
            host=config_parser.get_web_ui_host(config=config),
            port=config_parser.get_web_ui_port(config=config),
        )

    sync.sync(dry_run=args.dry_run, check_files=args.check_files)


if __name__ == "__main__":
    run()
