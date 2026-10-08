"""Inline-button taps built by the REAL python-telegram-bot SDK.

The gateway tests use MagicMock updates that invent ``callback_query.date``.
PTB's ``CallbackQuery`` has no ``date`` field at all, so with a real update the
tap reached samimizer with ``date=None`` and every "Stuur" was refused as
"carries no time". The adapter stamps the moment it received the tap instead,
and says so (``date_source="received"``). It never uses the card message's own
(old) date.
"""

import time

import pytest

telegram = pytest.importorskip("telegram")
if not hasattr(telegram, "__file__"):  # a MagicMock stand-in from another conftest
    pytest.skip("real python-telegram-bot required", allow_module_level=True)

from telegram import Update  # noqa: E402

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

CARD_DATE = 1000  # the card was sent long ago


def _real_tap(data="ok:abc:0123456789abcdef", callback_id="cb-real"):
    return Update.de_json({
        "update_id": 1,
        "callback_query": {
            "id": callback_id, "chat_instance": "ci",
            "from": {"id": 777, "is_bot": False, "first_name": "Sam"},
            "message": {"message_id": 456, "date": CARD_DATE,
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 1, "is_bot": True, "first_name": "Bot"}},
            "data": data,
        },
    }, None)


def _adapter():
    return object.__new__(TelegramAdapter)


def test_real_sdk_callback_query_has_no_date_field():
    """Pins the SDK fact the fix depends on; if PTB ever adds one, revisit."""
    assert not hasattr(_real_tap().callback_query, "date")


def test_real_tap_carries_receipt_time_not_card_date():
    before = int(time.time())
    event = _adapter()._normalize_platform_event(_real_tap())
    after = int(time.time())
    assert event is not None
    payload = event["payload"]
    assert isinstance(payload["date"], int)
    assert before <= payload["date"] <= after
    assert payload["date"] != CARD_DATE
    assert payload["date_source"] == "received"


def test_real_tap_keeps_identity_fields():
    payload = _adapter()._normalize_platform_event(_real_tap())["payload"]
    assert payload["user_id"] == "777"
    assert payload["chat_id"] == "123"
    assert payload["message_id"] == "456"
    assert payload["callback_query_id"] == "cb-real"
    assert payload["data"] == "ok:abc:0123456789abcdef"


def test_samimizer_plugin_accepts_the_real_tap_time():
    """The samimizer plugin's own event builder keeps the stamped time (it turns
    anything non-numeric into None, which the approval module refuses)."""
    import importlib.util
    from pathlib import Path

    plugin = Path("/home/ubuntu/Developer/repos/samimizer-live/hermes-plugin/samimizer-approvals/__init__.py")
    if not plugin.exists():
        pytest.skip("samimizer checkout not on this host")
    spec = importlib.util.spec_from_file_location("samimizer_approvals_probe", plugin)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    event = _adapter()._normalize_platform_event(_real_tap())
    ev = mod._callback_event(**event)
    assert ev is not None
    assert isinstance(ev["callback_query"]["date"], int)
