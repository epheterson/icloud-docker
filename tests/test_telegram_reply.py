"""Tests for the inbound-Telegram 2FA-reply feature.

Stacks on PR #464 (web UI). Reuses the existing outbound Telegram
config (bot_token + chat_id from app.notifications.telegram) plus a
new opt-in knob app.notifications.telegram.listen. Coverage:

  - config_parser.get_telegram_listen_enabled
  - notify.poll_telegram_for_code  (network mocked via patch on requests.post)
  - web_signals.record_telegram_offset / get_telegram_offset
  - sync._wait_for_telegram_code  (the polling loop + validate_2fa_code path)
  - sync._handle_2fa_required hands the api through when listen enabled

The 6-digit-code regex, chat_id filtering, offset advancement, and
debounce-via-offset are all exercised explicitly because each is a
plausible regression site.
"""

__author__ = "Mandar Patil (mandarons@pm.me)"

import unittest
from unittest.mock import MagicMock, patch

import tests  # noqa: F401 -- sets ENV_CONFIG_FILE_PATH


def _telegram_config(listen=True):
    """Minimal config dict with telegram outbound + optional listen."""
    return {
        "app": {
            "telegram": {
                "bot_token": "bot-x",
                "chat_id": "12345",
                **({"listen": True} if listen else {}),
            },
        },
    }


class TestGetTelegramListenEnabled(unittest.TestCase):
    """app.notifications.telegram.listen reader. Opt-in, default False."""

    def test_default_false(self):
        from src import config_parser

        self.assertFalse(config_parser.get_telegram_listen_enabled(config={}))
        self.assertFalse(
            config_parser.get_telegram_listen_enabled(
                config=_telegram_config(listen=False),
            ),
        )

    def test_true_when_set(self):
        from src import config_parser

        self.assertTrue(
            config_parser.get_telegram_listen_enabled(
                config=_telegram_config(listen=True),
            ),
        )


def _telegram_response(updates):
    """Build a fake Telegram getUpdates HTTP response."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"ok": True, "result": updates}
    return resp


def _update(update_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


class TestPollTelegramForText(unittest.TestCase):
    """notify.poll_telegram_for_text: returns next text, chat filter, offset, errors.

    The poller is intentionally dumb about content -- it returns the first text
    message from the configured chat verbatim; the caller (sync) classifies it
    as a trigger keyword vs a 6-digit code.
    """

    def test_returns_text_from_matching_chat(self):
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(10, 12345, "123456")])
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertEqual(text, "123456")
        self.assertEqual(new_offset, 10)

    def test_returns_any_text_not_just_digits(self):
        """Non-digit text (e.g. the 'auth' trigger) is returned verbatim --
        the caller decides what to do with it."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(7, 12345, "AUTH")])
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertEqual(text, "AUTH")
        self.assertEqual(new_offset, 7)

    def test_ignores_messages_from_other_chats(self):
        """Critical security check: only the configured chat is honoured.
        A different chat's message must NOT be returned."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response(
                [
                    _update(5, 99999, "123456"),  # wrong chat -- skipped
                    _update(6, 12345, "654321"),  # right chat
                ],
            )
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertEqual(text, "654321")
        self.assertEqual(new_offset, 6)

    def test_chat_id_compares_as_string(self):
        """Telegram returns chat.id as int OR string depending on origin;
        the filter has to tolerate both since config likely has a string."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(5, 12345, "123456")])
            # Pass chat_id as int -- should still match
            text, _ = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id=12345,
                offset=0,
            )
        self.assertEqual(text, "123456")

    def test_offset_is_the_returned_message_id(self):
        """Offset advances to the RETURNED message's id (not the highest),
        so the next poll continues to the following message -- important when
        a keyword and the code arrive in the same batch."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(42, 12345, "auth")])
            _, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertEqual(new_offset, 42)

    def test_first_text_wins_and_stops(self):
        """First text is returned + offset stops at it, so a following message
        (e.g. the code after the keyword) is read on the next poll, not skipped."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response(
                [
                    _update(1, 12345, "auth"),
                    _update(2, 12345, "111111"),
                ],
            )
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertEqual(text, "auth")
        self.assertEqual(new_offset, 1)  # stops here; next poll gets "111111"

    def test_no_text_advances_offset_past_noise(self):
        """Foreign-chat / non-text updates yield no text but advance the
        offset past them so they aren't re-processed."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(8, 99999, "spam")])
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 8)

    def test_skips_non_text_message_from_our_chat(self):
        """A non-text message (e.g. a photo) from our own chat yields no text
        but still advances the offset so it isn't re-processed next poll."""
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([_update(9, 12345, "")])
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=0,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 9)

    def test_network_error_returns_unchanged_offset(self):
        from src import notify

        with patch("src.notify.requests.post", side_effect=OSError("network down")):
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=15,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 15)

    def test_non_200_response_returns_unchanged_offset(self):
        from src import notify

        bad = MagicMock()
        bad.status_code = 401
        bad.text = "Unauthorized"
        with patch("src.notify.requests.post", return_value=bad):
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=15,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 15)

    def test_malformed_json_returns_unchanged_offset(self):
        from src import notify

        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        with patch("src.notify.requests.post", return_value=resp):
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=15,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 15)

    def test_empty_result_returns_unchanged_offset(self):
        from src import notify

        with patch("src.notify.requests.post") as post:
            post.return_value = _telegram_response([])
            text, new_offset = notify.poll_telegram_for_text(
                bot_token="bot-x",
                chat_id="12345",
                offset=15,
            )
        self.assertIsNone(text)
        self.assertEqual(new_offset, 15)


class TestWebSignalsTelegramOffset(unittest.TestCase):
    """web_signals telegram offset round trip + restart durability."""

    def setUp(self):
        from src import web_signals

        web_signals.record_telegram_offset(0)

    def test_round_trip(self):
        from src import web_signals

        web_signals.record_telegram_offset(42)
        self.assertEqual(web_signals.get_telegram_offset(), 42)

    def test_default_zero_when_never_recorded(self):
        from src import web_signals

        # setUp records 0; explicit re-state for clarity
        self.assertEqual(web_signals.get_telegram_offset(), 0)

    def test_malformed_persisted_offset_returns_zero(self):
        """Defensive: if the JSON state file is hand-edited to garbage
        in the offset field, get_telegram_offset returns 0 instead of
        raising."""
        from src import web_signals

        state = web_signals._load_state()  # noqa: SLF001
        key = web_signals._TELEGRAM_OFFSET_KEY  # noqa: SLF001
        state[key] = {"offset": "not-a-number"}
        web_signals._save_state(state)  # noqa: SLF001
        self.assertEqual(web_signals.get_telegram_offset(), 0)

    def test_record_coerces_to_int(self):
        from src import web_signals

        web_signals.record_telegram_offset("100")  # type: ignore[arg-type]
        self.assertEqual(web_signals.get_telegram_offset(), 100)


class TestWaitForTelegramCode(unittest.TestCase):
    """sync._wait_for_telegram_code: poll loop, validate, trust."""

    def setUp(self):
        from src import sync, web_signals

        web_signals.record_telegram_offset(0)
        # Mock the outbound messenger so the loop never hits the network in tests.
        tg = patch.object(sync, "_send_telegram_message")
        tg.start()
        self.addCleanup(tg.stop)

    def _api(self):
        api = MagicMock()
        api.validate_2fa_code.return_value = True
        return api

    def test_code_arrives_validates_and_trusts(self):
        from src import notify, sync

        api = self._api()
        with (
            patch.object(notify, "poll_telegram_for_text", return_value=("123456", 5)),
            patch("src.sync.sleep"),  # zero the wait
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=30,
            )
        self.assertTrue(result)
        api.validate_2fa_code.assert_called_once_with("123456")
        api.trust_session.assert_called_once()

    def test_no_code_within_timeout_returns_false(self):
        from src import notify, sync

        api = self._api()
        with (
            patch.object(notify, "poll_telegram_for_text", return_value=(None, 0)),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=60,
            )
        self.assertFalse(result)
        api.validate_2fa_code.assert_not_called()

    def test_rejected_code_keeps_polling(self):
        """validate_2fa_code returning False means Apple rejected the
        code -- the loop should continue polling, not exit."""
        from src import notify, sync

        api = self._api()
        api.validate_2fa_code.side_effect = [False, True]
        with (
            patch.object(
                notify,
                "poll_telegram_for_text",
                side_effect=[("111111", 1), ("222222", 2)],
            ),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=120,
            )
        self.assertTrue(result)
        self.assertEqual(api.validate_2fa_code.call_count, 2)

    def test_validate_raises_keeps_polling(self):
        """An exception in validate_2fa_code shouldn't abort the loop --
        it might be a transient Apple flake, give the next reply a shot."""
        from src import notify, sync

        api = self._api()
        api.validate_2fa_code.side_effect = [RuntimeError("flake"), True]
        with (
            patch.object(
                notify,
                "poll_telegram_for_text",
                side_effect=[("111111", 1), ("222222", 2)],
            ),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=120,
            )
        self.assertTrue(result)

    def test_trust_session_failure_still_returns_true(self):
        """trust_session is best-effort -- if it fails post-validation,
        the code STILL worked and we want to proceed."""
        from src import notify, sync

        api = self._api()
        api.trust_session.side_effect = RuntimeError("cookie write failed")
        with (
            patch.object(notify, "poll_telegram_for_text", return_value=("123456", 5)),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=30,
            )
        self.assertTrue(result)

    def test_missing_bot_token_falls_back_to_plain_sleep(self):
        from src import sync

        config = {
            "app": {
                "telegram": {"listen": True},  # no bot_token
            },
        }
        with patch("src.sync.sleep") as mock_sleep:
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=config,
                api=self._api(),
                timeout_seconds=30,
            )
        self.assertFalse(result)
        mock_sleep.assert_called_once_with(30)

    def test_offset_persisted_across_calls(self):
        """When poll returns a new offset, it's persisted so the next
        call starts from there -- the survives-restart contract."""
        from src import notify, sync, web_signals

        api = self._api()
        with (
            patch.object(notify, "poll_telegram_for_text", return_value=("123456", 99)),
            patch("src.sync.sleep"),
        ):
            sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=30,
            )
        self.assertEqual(web_signals.get_telegram_offset(), 99)

    def test_auth_keyword_triggers_2fa_push(self):
        """Replying 'auth' fires trigger_2fa_push_notification (so Apple
        actually pushes a code); a following 6-digit reply then validates."""
        from src import notify, sync

        api = self._api()
        with (
            patch.object(
                notify,
                "poll_telegram_for_text",
                side_effect=[("auth", 1), ("123456", 2)],
            ),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=120,
            )
        self.assertTrue(result)
        api.trigger_2fa_push_notification.assert_called_once()
        api.validate_2fa_code.assert_called_once_with("123456")

    def test_custom_auth_keyword_is_honoured(self):
        """A configured app.telegram.auth_keyword triggers the push, matched
        case-insensitively (multi-container disambiguation)."""
        from src import notify, sync

        api = self._api()
        config = _telegram_config()
        config["app"]["telegram"]["auth_keyword"] = "auth-photos"
        with (
            patch.object(
                notify,
                "poll_telegram_for_text",
                side_effect=[("Auth-Photos", 1), ("123456", 2)],
            ),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=config,
                api=api,
                timeout_seconds=120,
            )
        self.assertTrue(result)
        api.trigger_2fa_push_notification.assert_called_once()

    def test_auth_trigger_push_failure_is_non_fatal(self):
        """If trigger_2fa_push_notification raises, the loop logs it and keeps
        listening -- a later valid code still completes the re-auth."""
        from src import notify, sync

        api = self._api()
        api.trigger_2fa_push_notification.side_effect = RuntimeError("apple flake")
        with (
            patch.object(
                notify,
                "poll_telegram_for_text",
                side_effect=[("auth", 1), ("123456", 2)],
            ),
            patch("src.sync.sleep"),
        ):
            result = sync._wait_for_telegram_code(  # noqa: SLF001
                config=_telegram_config(),
                api=api,
                timeout_seconds=120,
            )
        self.assertTrue(result)


class TestSendTelegramMessage(unittest.TestCase):
    """sync._send_telegram_message: best-effort, never raises."""

    def test_swallows_network_errors(self):
        from src import sync

        with patch("src.sync.requests.post", side_effect=OSError("down")):
            sync._send_telegram_message("bot-x", "12345", "hi")  # noqa: SLF001

    def test_posts_to_sendmessage_endpoint(self):
        from src import sync

        with patch("src.sync.requests.post") as post:
            sync._send_telegram_message("bot-x", "12345", "hi")  # noqa: SLF001
        post.assert_called_once()
        self.assertIn("/botbot-x/sendMessage", post.call_args[0][0])


class TestHandle2faRequiredPassesApi(unittest.TestCase):
    """_handle_2fa_required threads ``api`` into the Telegram wait path
    when listen is enabled. When disabled, falls back to plain sleep."""

    def test_listen_enabled_calls_wait_for_telegram_code(self):
        from src import sync

        api = MagicMock()
        config = _telegram_config(listen=True)
        config["app"]["retry_login_interval"] = 60
        with (
            patch("src.sync._wait_for_telegram_code", return_value=True) as wait,
            patch("src.sync.notify.send", return_value=None),
            patch("src.sync._log_retry_time"),
            patch("src.sync.sleep") as mock_sleep,
        ):
            ok = sync._handle_2fa_required(  # noqa: SLF001
                config=config,
                username="u@e.com",
                sync_state=sync.SyncState(),
                api=api,
            )
        self.assertTrue(ok)
        wait.assert_called_once()
        mock_sleep.assert_not_called()

    def test_listen_disabled_uses_plain_sleep(self):
        from src import sync

        api = MagicMock()
        config = _telegram_config(listen=False)
        config["app"]["retry_login_interval"] = 60
        with (
            patch("src.sync._wait_for_telegram_code") as wait,
            patch("src.sync.notify.send", return_value=None),
            patch("src.sync._log_retry_time"),
            patch("src.sync.sleep") as mock_sleep,
        ):
            ok = sync._handle_2fa_required(  # noqa: SLF001
                config=config,
                username="u@e.com",
                sync_state=sync.SyncState(),
                api=api,
            )
        self.assertTrue(ok)
        wait.assert_not_called()
        mock_sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
