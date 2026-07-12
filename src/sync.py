"""Sync module."""

__author__ = "Mandar Patil <mandarons@pm.me>"
import datetime
import os
from time import sleep

from icloudpy import ICloudPyService, exceptions, utils

from src import (
    DEFAULT_CONFIG_FILE_PATH,
    ENV_CONFIG_FILE_PATH_KEY,
    ENV_ICLOUD_PASSWORD_KEY,
    config_parser,
    configure_icloudpy_logging,
    get_logger,
    live_photo_migration,
    notify,
    read_config,
    sync_drive,
    sync_photos,
)
from src.sync_stats import SyncSummary
from src.usage import alive

# Configure icloudpy logging immediately after import
configure_icloudpy_logging()

LOGGER = get_logger()


_TRUST_COOKIE_NAME = "X-APPLE-WEBAUTH-HSA-TRUST"


def _read_trust_cookie_expiry(api) -> datetime.datetime | None:
    """Return the expiry datetime of Apple's HSA trust cookie, or None.

    The trust window is carried by ``X-APPLE-WEBAUTH-HSA-TRUST`` in
    icloudpy's cookie jar (persisted to ``session_data/<username>`` as
    LWPCookieJar). Reading it directly avoids hardcoding Apple's trust
    duration -- the cookie's own ``expires`` field is the source of
    truth, set per-cookie by Apple's server. Returns None if the cookie
    isn't present (e.g. account never auth'd with 2FA, or trust cookie
    cleared).
    """
    try:
        cookies = api.session.cookies
    except AttributeError:
        return None
    for cookie in cookies:
        if cookie.name == _TRUST_COOKIE_NAME and cookie.expires:
            return datetime.datetime.fromtimestamp(
                cookie.expires,
                tz=datetime.timezone.utc,
            )
    return None


def _resolve_dashboard_url(config) -> str | None:
    """Compute the web UI URL to embed in notifications, or None.

    Returns ``None`` when ``app.web_ui.enabled`` is False -- callers
    fall back to the legacy docker-exec instruction. Otherwise prefers
    the explicit ``app.web_ui.public_url`` (e.g. the reverse-proxy
    URL); falls back to ``http://{host}:{port}`` with a warning logged
    once at startup if the public URL isn't set.
    """
    if not config_parser.get_web_ui_enabled(config=config):
        return None
    public_url = config_parser.get_web_ui_public_url(config=config)
    if public_url:
        return public_url
    host = config_parser.get_web_ui_host(config=config)
    port = config_parser.get_web_ui_port(config=config)
    # 0.0.0.0 / :: are bind-all addresses (what the server listens on), not
    # browsable destinations — surface loopback in the user-facing URL instead.
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    LOGGER.warning(
        "app.web_ui.public_url not set -- notification URLs will use "
        "http://%s:%s/, which won't work from outside the container. "
        "Set app.web_ui.public_url to your reverse-proxy URL.",
        host,
        port,
    )
    return f"http://{host}:{port}"


def _maybe_warn_trust_expiring(config, api, username: str) -> None:
    """Fire the trust-expiring notification once when crossing threshold.

    Reads the live trust cookie expiry, compares against
    ``app.trust_expiry_warn_days``, and -- if days_remaining is below
    the threshold AND we haven't already warned for THIS cookie value --
    fans the warning out through ``notify.send_trust_expiring``.

    Debounce key is the cookie expiry ISO string itself. When Apple
    refreshes the trust cookie (new expires_at), the stored
    ``warned_for_expires_at`` no longer matches and warning eligibility
    rearms automatically -- no manual reset needed.

    Best-effort: any exception is logged and swallowed so a notification
    bug never breaks the sync loop.
    """
    try:
        from src import notify, web_signals

        expires_at = _read_trust_cookie_expiry(api)
        expires_at_iso = expires_at.isoformat() if expires_at else None
        prior = web_signals.get_trust_state()
        web_signals.record_trust_state(
            expires_at_iso=expires_at_iso,
            warned_for_expires_at=prior.get("warned_for_expires_at"),
        )
        if expires_at is None:
            return
        days_remaining = (expires_at - datetime.datetime.now(tz=datetime.timezone.utc)).days
        threshold = config_parser.get_trust_expiry_warn_days(config=config)
        if days_remaining >= threshold:
            return
        if prior.get("warned_for_expires_at") == expires_at_iso:
            return  # already warned for this cookie value
        notify.send_trust_expiring(
            config=config,
            username=username,
            days_remaining=days_remaining,
            dashboard_url=_resolve_dashboard_url(config),
        )
        web_signals.record_trust_state(
            expires_at_iso=expires_at_iso,
            warned_for_expires_at=expires_at_iso,
        )
    except Exception as e:  # pragma: no cover - guarded so notify bugs don't break sync
        LOGGER.warning(f"trust-expiring check failed: {e!s}")


def get_api_instance(
    username: str,
    password: str,
    cookie_directory: str | None = None,
    server_region: str = "global",
) -> ICloudPyService:
    """
    Create and return an iCloud API client instance.

    Args:
        username: iCloud username/Apple ID
        password: iCloud password
        cookie_directory: Directory to store authentication cookies.
            When ``None`` (the default), resolved late from
            ``src.DEFAULT_COOKIE_DIRECTORY`` so test fixtures that
            redirect the constant at runtime take effect — the previous
            ``= DEFAULT_COOKIE_DIRECTORY`` default-arg capture made the
            constant unmockable post-import.
        server_region: Server region ("china" or "global")

    Returns:
        Configured ICloudPyService instance
    """
    if cookie_directory is None:
        # Read through the src module so monkey-patches of
        # ``src.DEFAULT_COOKIE_DIRECTORY`` (e.g. by tests/conftest.py)
        # are honoured. ``src`` is this function's parent package and
        # already imported; using ``sys.modules`` avoids a per-call
        # ``import src`` and makes the data flow explicit.
        import sys

        cookie_directory = sys.modules["src"].DEFAULT_COOKIE_DIRECTORY
    return (
        ICloudPyService(
            apple_id=username,
            password=password,
            cookie_directory=cookie_directory,
            home_endpoint="https://www.icloud.com.cn",
            setup_endpoint="https://setup.icloud.com.cn/setup/ws/1",
        )
        if server_region == "china"
        else ICloudPyService(
            apple_id=username,
            password=password,
            cookie_directory=cookie_directory,
        )
    )


class SyncState:
    """
    Maintains synchronization state for drive and photos.

    This class encapsulates the countdown timers and sync flags to avoid
    passing multiple variables between functions.
    """

    def __init__(self):
        """Initialize sync state with default values."""
        self.drive_time_remaining = 0
        self.photos_time_remaining = 0
        self.enable_sync_drive = True
        self.enable_sync_photos = True
        self.last_send = None


def _load_configuration():
    """
    Load configuration from file or environment.

    Returns:
        Configuration dictionary
    """
    config_path = os.environ.get(ENV_CONFIG_FILE_PATH_KEY, DEFAULT_CONFIG_FILE_PATH)
    return read_config(config_path=config_path)


def _extract_sync_intervals(config, log_messages: bool = False):
    """
    Extract drive and photos sync intervals from configuration.

    Args:
        config: Configuration dictionary
        log_messages: Whether to log informational messages (default: False for loop usage)

    Returns:
        tuple: (drive_sync_interval, photos_sync_interval)
    """
    drive_sync_interval = 0
    photos_sync_interval = 0

    if config and "drive" in config:
        drive_sync_interval = config_parser.get_drive_sync_interval(
            config=config,
            log_messages=log_messages,
        )
    if config and "photos" in config:
        photos_sync_interval = config_parser.get_photos_sync_interval(
            config=config,
            log_messages=log_messages,
        )

    return drive_sync_interval, photos_sync_interval


def _retrieve_password(username: str):
    """
    Retrieve password from environment or keyring.

    Args:
        username: iCloud username

    Returns:
        Password string or None if not found

    Raises:
        ICloudPyNoStoredPasswordAvailableException: If password not available
    """
    if ENV_ICLOUD_PASSWORD_KEY in os.environ:
        password = os.environ.get(ENV_ICLOUD_PASSWORD_KEY)
        utils.store_password_in_keyring(username=username, password=password)
        return password
    else:
        return utils.get_password_from_keyring(username=username)


def _authenticate_and_get_api(config, username: str):
    """
    Authenticate user and return iCloud API instance.

    Args:
        config: Configuration dictionary
        username: iCloud username

    Returns:
        ICloudPyService instance

    Raises:
        ICloudPyNoStoredPasswordAvailableException: If password not available
    """
    server_region = config_parser.get_region(config=config)
    password = _retrieve_password(username)
    return get_api_instance(
        username=username,
        password=password,
        server_region=server_region,
    )


def _check_mount_marker(
    destination_path: str,
    marker_filename: str,
    required: bool,
    service_name: str,
) -> bool:
    """Verify the failsafe marker file is present in a destination directory.

    Mirrors boredazfcuk/docker-icloudpd's ``.mounted`` pattern: protects
    against silent bind-mount failures (typo in the host path, missing
    share, wrong permissions) that would otherwise dump iCloud data into
    an empty container-internal directory.

    Returns True when it is safe to proceed (marker not required, or marker
    required and present). Returns False when the marker is required but
    absent — in which case the caller should skip this sync cycle without
    advancing the countdown so the next interval re-checks.

    Args:
        destination_path: Sync destination directory.
        marker_filename: Filename to look for inside ``destination_path``
            (e.g. ``.mounted``).
        required: Whether the marker is required at all. When False this
            is a no-op that always returns True.
        service_name: Human-readable label used in the error log
            (``Drive`` / ``Photos``).

    Returns:
        True if it is safe to proceed; False to skip this sync cycle.
    """
    if not required:
        return True
    marker_path = os.path.join(destination_path, marker_filename)
    if os.path.isfile(marker_path):
        return True
    LOGGER.error(
        f"{service_name} mount marker missing: {marker_path} not found — "
        f"refusing to sync. Create the marker file (`touch {marker_path}`) "
        f"after confirming the destination is correctly mounted, then the "
        f"next sync cycle will proceed.",
    )
    return False


def _perform_drive_sync(config, api, sync_state: SyncState, drive_sync_interval: int):
    """
    Execute drive synchronization if enabled.

    Args:
        config: Configuration dictionary
        api: iCloud API instance
        sync_state: Current sync state
        drive_sync_interval: Drive sync interval in seconds

    Returns:
        DriveStats object if sync was performed, None otherwise
    """
    if config and "drive" in config and sync_state.enable_sync_drive:
        import time

        from src.sync_stats import DriveStats

        start_time = time.time()
        stats = DriveStats()

        destination_path = config_parser.prepare_drive_destination(config=config)

        # Mount-marker failsafe (see _check_mount_marker). Skip this cycle
        # without advancing the countdown so the next interval re-checks
        # once the user fixes the mount + touches the marker file.
        if not _check_mount_marker(
            destination_path=destination_path,
            marker_filename=config_parser.get_mount_marker_filename(config=config),
            required=config_parser.get_drive_require_mount_marker(config=config),
            service_name="Drive",
        ):
            return None

        # Count files before sync
        files_before = set()
        if os.path.exists(destination_path):
            try:
                for root, _dirs, file_list in os.walk(destination_path):
                    for file in file_list:
                        files_before.add(os.path.join(root, file))
            except Exception:
                pass

        LOGGER.info("Syncing drive...")
        try:
            from src import web_signals as _ws

            _ws.record_sync_started("drive")
        except ImportError:
            pass
        files_after = sync_drive.sync_drive(config=config, drive=api.drive)
        LOGGER.info("Drive synced")

        # Calculate statistics
        stats.duration_seconds = time.time() - start_time

        # Handle case where sync_drive returns None (e.g., in tests)
        if files_after is not None:
            # Count newly downloaded files
            new_files = files_after - files_before
            stats.files_downloaded = len(new_files)

            # Count skipped files
            stats.files_skipped = len(files_before & files_after)

            # Count removed files
            if config_parser.get_drive_remove_obsolete(config=config):
                stats.files_removed = len(files_before - files_after)

            # Calculate bytes downloaded
            try:
                for file_path in new_files:
                    if os.path.exists(file_path) and os.path.isfile(file_path):
                        stats.bytes_downloaded += os.path.getsize(file_path)
            except Exception:
                pass

        # Reset countdown timer to the configured interval
        sync_state.drive_time_remaining = drive_sync_interval
        return stats
    return None


def _perform_photos_sync(config, api, sync_state: SyncState, photos_sync_interval: int):
    """
    Execute photos synchronization if enabled.

    Args:
        config: Configuration dictionary
        api: iCloud API instance
        sync_state: Current sync state
        photos_sync_interval: Photos sync interval in seconds

    Returns:
        PhotoStats object if sync was performed, None otherwise
    """
    if config and "photos" in config and sync_state.enable_sync_photos:
        import time

        from src.sync_stats import PhotoStats

        start_time = time.time()
        stats = PhotoStats()

        destination_path = config_parser.prepare_photos_destination(config=config)

        # Mount-marker failsafe (see _check_mount_marker). Skip this cycle
        # without advancing the countdown so the next interval re-checks
        # once the user fixes the mount + touches the marker file.
        if not _check_mount_marker(
            destination_path=destination_path,
            marker_filename=config_parser.get_mount_marker_filename(config=config),
            required=config_parser.get_photos_require_mount_marker(config=config),
            service_name="Photos",
        ):
            return None

        # Count files before sync
        files_before = set()
        if os.path.exists(destination_path):
            try:
                for root, _dirs, file_list in os.walk(destination_path):
                    for file in file_list:
                        files_before.add(os.path.join(root, file))
            except Exception:
                pass

        LOGGER.info("Syncing photos...")
        try:
            from src import web_signals as _ws

            _ws.record_sync_started("photos")
        except ImportError:
            pass
        sync_result = sync_photos.sync_photos(config=config, photos=api.photos)
        LOGGER.info("Photos synced")

        # Count files after sync
        files_after = set()
        if os.path.exists(destination_path):
            try:
                for root, _dirs, file_list in os.walk(destination_path):
                    for file in file_list:
                        files_after.add(os.path.join(root, file))
            except Exception:
                pass

        # Calculate statistics
        stats.duration_seconds = time.time() - start_time

        # Count newly downloaded files
        new_files = files_after - files_before
        stats.photos_downloaded = len(new_files)

        # Estimate hardlinked photos (approximate)
        use_hardlinks = config_parser.get_photos_use_hardlinks(
            config=config,
            log_messages=False,
        )
        if use_hardlinks:
            stats.photos_hardlinked = max(
                0,
                len(files_after) - len(files_before) - stats.photos_downloaded,
            )

        # Count skipped photos
        stats.photos_skipped = len(files_before & files_after)

        # Calculate bytes downloaded
        try:
            for file_path in new_files:
                if os.path.exists(file_path) and os.path.isfile(file_path):
                    stats.bytes_downloaded += os.path.getsize(file_path)

            # Estimate bytes saved by hardlinks
            if use_hardlinks and stats.photos_hardlinked > 0:
                for file_path in files_after:
                    if file_path not in new_files and os.path.isfile(file_path):
                        stats.bytes_saved_by_hardlinks += os.path.getsize(file_path)
        except Exception:
            pass

        # Track failed downloads so notifications reflect errors
        if isinstance(sync_result, tuple):
            _, failed_downloads = sync_result
            if failed_downloads > 0:
                stats.errors.append(f"{failed_downloads} photo download(s) failed")

        # Get list of synced albums (simple approximation based on directories)
        try:
            for item in os.listdir(destination_path):
                item_path = os.path.join(destination_path, item)
                if os.path.isdir(item_path):
                    stats.albums_synced.append(item)
        except Exception:
            pass

        # Reset countdown timer to the configured interval
        sync_state.photos_time_remaining = photos_sync_interval
        return stats
    return None


def _perform_dry_run(config, api, check_files: int | None = None) -> None:
    """Authenticate-and-enumerate path used when ``--dry-run`` is passed.

    Verifies that the configured credentials, mount paths, and iCloud-side
    state are all in working order WITHOUT writing or downloading any
    files. Designed as the safety check users run before letting the real
    sync loop loose on a new install.

    Logs (at INFO level):
      - Drive destination path + root-level item count (when Drive is configured)
      - Photos destination path + library names (when Photos is configured)
      - When ``check_files`` is not None: per-library would-skip /
        size-mismatch / not-found counts (see ``migration_check``).

    Args:
        config: Configuration dictionary
        api: Authenticated iCloud API instance
        check_files: When set (``--check-files=N``), additionally walks
            up to N photos per library and reports what a real sync
            would do per file. ``0`` walks every photo. ``None`` skips
            this check (cheap default for ``--dry-run`` alone).

    Notifications, usage statistics, file writes, file deletions, and the
    sync loop itself are all skipped.
    """
    LOGGER.info("DRY RUN: authentication succeeded — verifying configured services.")

    if config and "drive" in config:
        try:
            drive_destination = config_parser.get_drive_destination_path(config=config)
            LOGGER.info(f"DRY RUN: Drive destination: {drive_destination}")
            root_items = list(api.drive.dir())
            LOGGER.info(
                f"DRY RUN: Drive root contains {len(root_items)} item(s) — "
                "real sync would walk this tree per `drive.filters`.",
            )
        except Exception as e:
            LOGGER.warning(f"DRY RUN: Drive enumeration failed: {e!s}")
    else:
        LOGGER.info(
            "DRY RUN: no `drive:` section in config — Drive sync would be skipped.",
        )

    if config and "photos" in config:
        try:
            photos_destination = config_parser.get_photos_destination_path(
                config=config,
            )
            LOGGER.info(f"DRY RUN: Photos destination: {photos_destination}")
            libraries = list(api.photos.libraries.keys()) if hasattr(api.photos, "libraries") else []
            if libraries:
                LOGGER.info(
                    f"DRY RUN: Photos libraries available: {', '.join(libraries)}",
                )
            else:
                LOGGER.info("DRY RUN: Photos libraries: (none reported by iCloud)")
        except Exception as e:
            LOGGER.warning(f"DRY RUN: Photos enumeration failed: {e!s}")
    else:
        LOGGER.info(
            "DRY RUN: no `photos:` section in config — Photos sync would be skipped.",
        )

    if check_files is not None:
        from src import migration_check

        # Photos walker — per-library counts using mandarons' real path/size
        # logic so the report mirrors what a real sync would skip vs download.
        if config and "photos" in config:
            try:
                LOGGER.info(
                    f"DRY RUN: walking photos for file-existence check "
                    f"(--check-files={'all' if check_files == 0 else check_files} per library) ...",
                )
                results = migration_check.check_migration(
                    api=api,
                    config=config,
                    sample=check_files,
                )
                for library_name, result in results.items():
                    stats = result["stats"]
                    LOGGER.info(
                        f"DRY RUN: {library_name} (dest {result['library_dest']}): "
                        f"sampled={result['checked']} "
                        f"would_skip={stats['would_skip']} "
                        f"size_mismatch={stats['size_mismatch']} "
                        f"not_found={stats['not_found']} "
                        f"errors={stats['error']}",
                    )
                    for status, items in result["samples"].items():
                        for item in items:
                            if status == "size_mismatch":
                                path, expected, actual = item
                                LOGGER.info(
                                    f"DRY RUN:   sample {status}: {path} (have {actual:,}b, want {expected:,}b)",
                                )
                            else:
                                path, expected = item
                                LOGGER.info(
                                    f"DRY RUN:   sample {status}: {path} ({expected:,}b)",
                                )
            except Exception as e:
                LOGGER.warning(f"DRY RUN: photos check-files walk failed: {e!s}")

        # Drive walker — same per-file would_skip/size_mismatch/not_found
        # report, but walking the Drive tree (no library_destinations,
        # mirror-tree layout). Catches misconfigured drive.destination.
        if config and "drive" in config:
            try:
                drive_result = migration_check.check_drive_migration(
                    api=api,
                    config=config,
                    sample=check_files,
                )
                if drive_result is not None:
                    stats = drive_result["stats"]
                    LOGGER.info(
                        f"DRY RUN: Drive (dest {drive_result['drive_destination']}): "
                        f"sampled={drive_result['checked']} "
                        f"would_skip={stats['would_skip']} "
                        f"size_mismatch={stats['size_mismatch']} "
                        f"not_found={stats['not_found']} "
                        f"errors={stats['error']}",
                    )
                    for status, items in drive_result["samples"].items():
                        for item in items:
                            if status == "size_mismatch":
                                path, expected, actual = item
                                LOGGER.info(
                                    f"DRY RUN:   sample {status}: {path} (have {actual:,}b, want {expected:,}b)",
                                )
                            else:
                                path, expected = item
                                LOGGER.info(
                                    f"DRY RUN:   sample {status}: {path} ({expected:,}b)",
                                )
            except Exception as e:
                LOGGER.warning(f"DRY RUN: drive check-files walk failed: {e!s}")

    LOGGER.info(
        "DRY RUN complete — no files were written. Re-run without --dry-run to sync.",
    )


def _check_services_configured(config):
    """
    Check if any sync services are configured.

    Args:
        config: Configuration dictionary

    Returns:
        bool: True if at least one service is configured
    """

    return "drive" in config or "photos" in config


def _send_usage_statistics(config, summary: SyncSummary) -> None:
    """Send anonymized usage statistics.

    Args:
        config: Configuration dictionary
        summary: Sync summary with statistics
    """

    # Create anonymized usage data
    usage_data = {
        "sync_duration": (
            (summary.sync_end_time - summary.sync_start_time).total_seconds() if summary.sync_end_time else 0
        ),
        "has_drive_activity": bool(
            summary.drive_stats and summary.drive_stats.has_activity(),
        ),
        "has_photos_activity": bool(
            summary.photo_stats and summary.photo_stats.has_activity(),
        ),
        "has_errors": summary.has_errors(),
        "timestamp": (summary.sync_end_time.isoformat() if summary.sync_end_time else None),
    }

    # Add aggregated statistics (no personal data)
    if summary.drive_stats:
        usage_data["drive"] = {
            "files_count": summary.drive_stats.files_downloaded,
            "bytes_count": summary.drive_stats.bytes_downloaded,
            "has_errors": summary.drive_stats.has_errors(),
        }

    if summary.photo_stats:
        usage_data["photos"] = {
            "photos_count": summary.photo_stats.photos_downloaded,
            "bytes_count": summary.photo_stats.bytes_downloaded,
            "hardlinks_count": summary.photo_stats.photos_hardlinked,
            "has_errors": summary.photo_stats.has_errors(),
        }

    # Send to usage tracking
    alive(config=config, data=usage_data)


def _handle_2fa_required(config, username: str, sync_state: SyncState, api=None):
    """
    Handle 2FA authentication requirement.

    Args:
        config: Configuration dictionary
        username: iCloud username
        sync_state: Current sync state
        api: Live ICloudPyService instance still in requires_2fa state.
            When provided AND ``app.notifications.telegram.listen`` is
            true, the sleep gap is replaced with a polling loop that
            accepts a 6-digit code reply from the configured Telegram
            chat and feeds it to ``api.validate_2fa_code`` directly --
            no trip to the web UI needed. Returns early on success so
            the next outer-loop iteration finds the now-trusted session.

    Returns:
        bool: True if should continue (retry), False if should exit
    """
    LOGGER.error("Error: 2FA is required. Please log in.")
    sleep_for = config_parser.get_retry_login_interval(config=config)

    if sleep_for < 0:
        LOGGER.info("retry_login_interval is < 0, exiting ...")
        return False

    _log_retry_time(sleep_for)
    server_region = config_parser.get_region(config=config)
    sync_state.last_send = notify.send(
        config=config,
        username=username,
        last_send=sync_state.last_send,
        region=server_region,
        dashboard_url=_resolve_dashboard_url(config),
    )
    if api is not None and config_parser.get_telegram_listen_enabled(config=config):
        _wait_for_telegram_code(config=config, api=api, timeout_seconds=sleep_for)
    else:
        sleep(sleep_for)
    return True


def _send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    """Best-effort one-off Telegram message (instructions / confirmations)."""
    import requests

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        LOGGER.warning(f"telegram sendMessage failed: {e!s}")


def _wait_for_telegram_code(config, api, timeout_seconds: int) -> bool:
    """Drive 2FA over Telegram with a MANUAL trigger (nothing auto-fires).

    Flow -- the user initiates each step:
      1. Prompt the user to reply ``auth``.
      2. On ``auth`` (case-insensitive) -> ``api.trigger_2fa_push_notification()``
         so Apple actually pushes a code to the trusted devices, then ask for the
         6 digits. (The headless path previously listened for a code it never
         requested -- this is the missing trigger.)
      3. On a 6-digit reply -> ``validate_2fa_code`` + ``trust_session``.

    Returns True once a code validates + trust succeeds within ``timeout_seconds``;
    False on timeout. Best-effort throughout. Offset persisted via web_signals.
    """
    import re as _re

    poll_interval = 15
    from src import web_signals

    bot_token = config_parser.get_telegram_bot_token(config=config)
    chat_id = config_parser.get_telegram_chat_id(config=config)
    auth_keyword = config_parser.get_telegram_auth_keyword(config=config)
    if not bot_token or not chat_id:
        LOGGER.warning(
            "Telegram listen enabled but bot_token/chat_id not configured; falling back to plain sleep.",
        )
        sleep(timeout_seconds)
        return False

    _send_telegram_message(
        bot_token,
        chat_id,
        f"🔐 iCloud needs re-authentication. Reply '{auth_keyword}' and I'll send a 2FA "
        "code to your Apple devices; then reply the 6-digit code here.",
    )
    LOGGER.info(
        f"Listening on Telegram for '{auth_keyword}' trigger or 6-digit code (timeout {timeout_seconds}s).",
    )
    elapsed = 0
    while elapsed < timeout_seconds:
        chunk = min(poll_interval, timeout_seconds - elapsed)
        sleep(chunk)
        elapsed += chunk
        offset = web_signals.get_telegram_offset()
        text, new_offset = notify.poll_telegram_for_text(
            bot_token=bot_token,
            chat_id=chat_id,
            offset=offset,
        )
        if new_offset != offset:
            web_signals.record_telegram_offset(new_offset)
        if not text:
            continue
        norm = text.strip().lower()
        if norm == auth_keyword:
            LOGGER.info("Telegram auth trigger received -- requesting 2FA push.")
            try:
                pushed = api.trigger_2fa_push_notification()
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(f"trigger_2fa_push_notification raised: {e!s}")
                pushed = False
            _send_telegram_message(
                bot_token,
                chat_id,
                (
                    "✅ 2FA code sent to your Apple devices -- reply the 6-digit code here."
                    if pushed
                    else "⚠️ Couldn't request a code (no trusted device, or auth state off). Try icloud.zosia.io/auth."
                ),
            )
            continue
        if _re.fullmatch(r"\d{6}", norm):
            LOGGER.info("Received 6-digit code via Telegram -- validating.")
            try:
                accepted = api.validate_2fa_code(norm)
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(
                    f"validate_2fa_code raised: {e!s} -- waiting for another code.",
                )
                continue
            if not accepted:
                _send_telegram_message(
                    bot_token,
                    chat_id,
                    "❌ Apple rejected that code -- reply a fresh one.",
                )
                LOGGER.warning(
                    "Apple rejected the Telegram-supplied code -- waiting for another.",
                )
                continue
            try:
                api.trust_session()
            except Exception as e:  # noqa: BLE001
                LOGGER.warning(f"trust_session raised (non-fatal): {e!s}")
            _send_telegram_message(
                bot_token,
                chat_id,
                "✅ Re-authenticated. iCloud sync resumed.",
            )
            LOGGER.info("Telegram-driven 2FA succeeded; resuming sync.")
            return True
    LOGGER.info(
        "Telegram listen timeout reached with no usable code; retrying auth.",
    )
    return False


def _handle_password_error(config, username: str, sync_state: SyncState):
    """
    Handle password not available error.

    Args:
        config: Configuration dictionary
        username: iCloud username
        sync_state: Current sync state

    Returns:
        bool: True if should continue (retry), False if should exit
    """
    LOGGER.error(
        "Password is not stored in keyring. Please save the password in keyring.",
    )
    sleep_for = config_parser.get_retry_login_interval(config=config)

    if sleep_for < 0:
        LOGGER.info("retry_login_interval is < 0, exiting ...")
        return False

    _log_retry_time(sleep_for)
    server_region = config_parser.get_region(config=config)
    sync_state.last_send = notify.send(
        config=config,
        username=username,
        last_send=sync_state.last_send,
        region=server_region,
        dashboard_url=_resolve_dashboard_url(config),
    )
    sleep(sleep_for)
    return True


def _log_retry_time(sleep_for: int):
    """
    Log the next retry time.

    Args:
        sleep_for: Sleep duration in seconds
    """
    next_sync = (datetime.datetime.now() + datetime.timedelta(seconds=sleep_for)).strftime("%c")
    LOGGER.info(f"Retrying login at {next_sync} ...")


def _calculate_next_sync_schedule(config, sync_state: SyncState):
    """
    Calculate next sync schedule and update sync state.

    This function implements the adaptive scheduling algorithm that determines
    which service should sync next based on countdown timers.

    Args:
        config: Configuration dictionary
        sync_state: Current sync state

    Returns:
        int: Sleep duration in seconds
    """
    has_drive = config and "drive" in config
    has_photos = config and "photos" in config

    if not has_drive and has_photos:
        sleep_for = sync_state.photos_time_remaining
        sync_state.enable_sync_drive = False
        sync_state.enable_sync_photos = True
    elif has_drive and not has_photos:
        sleep_for = sync_state.drive_time_remaining
        sync_state.enable_sync_drive = True
        sync_state.enable_sync_photos = False
    elif has_drive and has_photos and sync_state.drive_time_remaining <= sync_state.photos_time_remaining:
        # Special case: if both timers are equal and large (> 10 seconds), wait for the full interval
        # This fixes the bug where equal large intervals cause immediate re-sync
        if sync_state.drive_time_remaining == sync_state.photos_time_remaining and sync_state.drive_time_remaining > 10:
            sleep_for = sync_state.drive_time_remaining
            sync_state.enable_sync_drive = True
            sync_state.enable_sync_photos = True
        else:
            sleep_for = sync_state.photos_time_remaining - sync_state.drive_time_remaining
            sync_state.photos_time_remaining -= sync_state.drive_time_remaining
            sync_state.enable_sync_drive = True
            sync_state.enable_sync_photos = False
    else:
        sleep_for = sync_state.drive_time_remaining - sync_state.photos_time_remaining
        sync_state.drive_time_remaining -= sync_state.photos_time_remaining
        sync_state.enable_sync_drive = False
        sync_state.enable_sync_photos = True

    return sleep_for


def _log_next_sync_time(sleep_for: int):
    """
    Log the next scheduled sync time.

    Args:
        sleep_for: Sleep duration in seconds
    """
    next_sync = (datetime.datetime.now() + datetime.timedelta(seconds=sleep_for)).strftime("%c")
    LOGGER.info(f"Resyncing at {next_sync} ...")


def _log_sync_intervals_at_startup(config):
    """
    Log sync intervals once at startup.

    Args:
        config: Configuration dictionary
    """
    if config and "drive" in config:
        config_parser.get_drive_sync_interval(config=config, log_messages=True)
    if config and "photos" in config:
        config_parser.get_photos_sync_interval(config=config, log_messages=True)


def _should_exit_oneshot_mode(config):
    """
    Check if should exit in oneshot mode.

    Oneshot mode exits when ALL configured sync intervals are negative.

    Args:
        config: Configuration dictionary

    Returns:
        bool: True if should exit
    """

    should_exit_drive = ("drive" not in config) or (
        config_parser.get_drive_sync_interval(config=config, log_messages=False) < 0
    )
    should_exit_photos = ("photos" not in config) or (
        config_parser.get_photos_sync_interval(config=config, log_messages=False) < 0
    )

    return should_exit_drive and should_exit_photos


def _run_live_photo_migration_if_configured(config):
    """Run the one-shot mislabeled Live Photo video migration if enabled.

    Renames pre-existing ``.HEIC``-labeled Live Photo videos to ``.MOV`` once at
    startup. No-op unless ``photos.migrate_mislabeled_live_videos`` is set to
    ``dry-run`` or ``apply``. Idempotent, so it is safe on every restart.
    """
    if not config or "photos" not in config:
        return
    mode = config_parser.get_photos_migrate_mislabeled_live_videos(config)
    if mode == "off":
        return
    destination = config_parser.prepare_photos_destination(config)
    live_photo_migration.run_migration([destination], mode)


def sync(dry_run: bool = False, check_files: int | None = None):
    """
    Main synchronization loop.

    Orchestrates the entire sync process by delegating specific responsibilities
    to focused helper functions. This function coordinates the high-level flow
    while each helper handles a single concern.

    Args:
        dry_run: When True, authenticate and summarise what would be synced,
            then exit without writing files, sending notifications, or
            entering the sync loop. Useful for verifying credentials, mount
            paths, and config before the real loop starts downloading.
        check_files: Optional sample size for the per-photo file-existence
            check during dry-run. Only meaningful with ``dry_run=True``.
            ``None`` skips the check (cheap default). ``0`` walks every
            photo (slow on large libraries). Positive N walks N
            stride-sampled photos per library.
    """
    sync_state = SyncState()
    startup_logged = False

    while True:
        config = _load_configuration()
        alive(config=config)

        # Log sync intervals once at startup
        if not startup_logged:
            _log_sync_intervals_at_startup(config)
            _run_live_photo_migration_if_configured(config)
            startup_logged = True

        drive_sync_interval, photos_sync_interval = _extract_sync_intervals(
            config,
            log_messages=False,
        )
        username = config_parser.get_username(config=config) if config else None

        # Web UI "Sync now" requests: ``src.web_signals`` writes a
        # sentinel file when the user taps the button; we delete it and
        # zero the countdown so the next pass through the sync calls
        # runs immediately. Best-effort import so vanilla mandarons
        # builds without the web-UI module still work.
        try:
            from src import web_signals as _ws

            if _ws.consume_force_sync("drive"):
                LOGGER.info("Force-sync requested for Drive — running immediately")
                sync_state.drive_time_remaining = 0
            if _ws.consume_force_sync("photos"):
                LOGGER.info("Force-sync requested for Photos — running immediately")
                sync_state.photos_time_remaining = 0
        except ImportError:
            pass

        if username:
            try:
                api = _authenticate_and_get_api(config, username)

                # Dry-run path: authenticate, enumerate, log, exit.
                # Skips the entire sync + notification + retry pipeline.
                if dry_run:
                    if api.requires_2sa:
                        LOGGER.info(
                            "DRY RUN: 2FA required — finish interactive auth first "
                            "(see README), then re-run with --dry-run.",
                        )
                    else:
                        _perform_dry_run(config, api, check_files=check_files)
                    return

                if not api.requires_2sa:
                    # Trust-window check: record current cookie expiry and
                    # fire a pre-emptive warning once if it's about to lapse.
                    # Best-effort: any failure is logged + swallowed inside.
                    _maybe_warn_trust_expiring(config, api, username)

                    # Create summary for this sync cycle
                    summary = SyncSummary()

                    # Perform syncs and collect statistics
                    drive_stats = _perform_drive_sync(
                        config,
                        api,
                        sync_state,
                        drive_sync_interval,
                    )
                    photos_stats = _perform_photos_sync(
                        config,
                        api,
                        sync_state,
                        photos_sync_interval,
                    )

                    # Populate summary with statistics
                    summary.drive_stats = drive_stats
                    summary.photo_stats = photos_stats
                    summary.sync_end_time = datetime.datetime.now()

                    # Persist per-service last-sync state for the web
                    # dashboard. Best-effort — if the JSON write fails
                    # the sync itself is unaffected.
                    try:
                        from src import web_signals as _ws

                        if drive_stats is not None:
                            _ws.record_sync_completion(
                                service="drive",
                                files_downloaded=drive_stats.files_downloaded,
                                files_skipped=drive_stats.files_skipped,
                                files_removed=drive_stats.files_removed,
                                errors=len(drive_stats.errors),
                                duration_seconds=drive_stats.duration_seconds,
                                bytes_downloaded=drive_stats.bytes_downloaded,
                            )
                        if photos_stats is not None:
                            _ws.record_sync_completion(
                                service="photos",
                                files_downloaded=photos_stats.photos_downloaded,
                                files_skipped=photos_stats.photos_skipped,
                                errors=len(photos_stats.errors),
                                duration_seconds=photos_stats.duration_seconds,
                                bytes_downloaded=photos_stats.bytes_downloaded,
                            )
                    except ImportError:
                        pass
                    except Exception as e:
                        LOGGER.debug(
                            f"web_signals: record_sync_completion raised: {e!s}",
                        )

                    # Send usage statistics (anonymized summary data)
                    try:
                        _send_usage_statistics(config, summary)
                    except Exception as e:
                        LOGGER.debug(f"Failed to send usage statistics: {e!s}")

                    # Send sync summary notification if configured
                    # Only send notification when both enabled services have synced in this cycle
                    # Gracefully handle notification failures to not break sync
                    has_drive_config = config and "drive" in config
                    has_photos_config = config and "photos" in config

                    should_send_notification = False
                    if has_drive_config and has_photos_config:
                        # Both services configured - send notification only when both have synced
                        should_send_notification = drive_stats is not None and photos_stats is not None
                    elif has_drive_config and not has_photos_config:
                        # Only drive configured - send when drive synced
                        should_send_notification = drive_stats is not None
                    elif has_photos_config and not has_drive_config:
                        # Only photos configured - send when photos synced
                        should_send_notification = photos_stats is not None

                    if should_send_notification:
                        try:
                            notify.send_sync_summary(config=config, summary=summary)
                        except Exception as e:
                            LOGGER.debug(
                                f"Failed to send sync summary notification: {e!s}",
                            )

                    if not _check_services_configured(config):
                        LOGGER.warning(
                            "Nothing to sync. Please add drive: and/or photos: section in config.yaml file.",
                        )
                else:
                    # Pass the live api so the Telegram-listen path can
                    # call validate_2fa_code on this exact session.
                    if not _handle_2fa_required(config, username, sync_state, api=api):
                        break
                    continue

            except exceptions.ICloudPyNoStoredPasswordAvailableException:
                if not _handle_password_error(config, username, sync_state):
                    break
                continue

        sleep_for = _calculate_next_sync_schedule(config, sync_state)
        _log_next_sync_time(sleep_for)

        if _should_exit_oneshot_mode(config):
            LOGGER.info(
                "All configured sync intervals are negative, exiting oneshot mode...",
            )
            break

        sleep(sleep_for)
