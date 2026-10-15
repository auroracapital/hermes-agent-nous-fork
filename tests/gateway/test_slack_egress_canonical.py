"""Real guard/broker protocol around SDK-name translation, with mocked Slack."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.egress_guard import EgressDenied, claim
from tests.slack_egress_fixture import slack_egress_broker  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat_postMessage", "chat_update"])
async def test_canonical_sdk_method_requires_exact_one_use_grant(slack_egress_broker, method):
    adapter = object.__new__(SlackAdapter)
    call = AsyncMock(return_value={"ok": True})
    client = SimpleNamespace(**{method: call})

    async def send(text="approved text", channel="C1"):
        return await adapter._call_with_block_fallback(
            lambda: client, method, {"channel": channel, "text": text}, "send")

    assert not slack_egress_broker.caller_grant("C1", "approved text")["ok"]
    with pytest.raises(EgressDenied, match="no pre-recorded grant"):
        await send()
    call.assert_not_awaited()
    slack_egress_broker.grant("C1", "approved text")
    with pytest.raises(EgressDenied, match="no pre-recorded grant"):
        await send("different text")
    with pytest.raises(EgressDenied, match="no pre-recorded grant"):
        await send(channel="C2")
    call.assert_not_awaited()
    assert await send() == {"ok": True}
    call.assert_awaited_once_with(channel="C1", text="approved text")
    with pytest.raises(EgressDenied, match="already consumed"):
        await send()
    assert call.await_count == 1


@pytest.mark.asyncio
async def test_unknown_method_never_calls_slack(slack_egress_broker):
    adapter = object.__new__(SlackAdapter)
    client = SimpleNamespace(chat_destroy=AsyncMock())
    slack_egress_broker.grant("C1", "approved text")
    with pytest.raises(EgressDenied, match="unknown tool"):
        await adapter._call_with_block_fallback(
            lambda: client, "chat_destroy", {"channel": "C1", "text": "approved text"}, "send")
    client.chat_destroy.assert_not_awaited()


def test_missing_broker_still_denies(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_EGRESS_BROKER_SOCK", "/tmp/nonexistent-hermes-canonical-test.sock")
    with pytest.raises(EgressDenied, match="broker unreachable"):
        claim("C1", "C1", "text", approved=True)
