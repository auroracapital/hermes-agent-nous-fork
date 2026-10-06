"""A stale-killed stream that produced no output fails over instead of retrying the same model.

On a loopback CLIProxy endpoint the stale detector waits 900s per attempt and the stream
retries the same silent model three times before the turn loop even considers fallback
(observed 2026-09-13: kimi-latest quota exhausted upstream, proxy held the stream open
with HTTP 200 and no bytes; 45 minutes per API call). With a fallback provider pending,
the first zero-output stale kill now raises ``StaleStreamNoOutputError`` and
``turn_recovery`` activates fallback immediately.
"""
from unittest.mock import MagicMock

import httpx
import pytest

from agent.chat_completion_helpers import _StreamingCall
from agent.error_classifier import FailoverReason, classify_api_error
from agent.errors import StaleStreamNoOutputError


def _call(*, pending_fallback=True, stale_killed=True, deltas_sent=False):
    agent = MagicMock()
    agent._has_pending_fallback.return_value = pending_fallback
    agent._is_provider_stream_parse_error.return_value = False
    agent._stream_options_unsupported = True  # skip the stream_options compat branch
    call = _StreamingCall(agent, {"model": "kimi-latest"}, None)
    call._stream_stale_timeout = 300.0
    call.stream_attempt_state["current"] = 1
    if stale_killed:
        call.stale_killed_attempt["id"] = 1
    call.deltas_were_sent["yes"] = deltas_sent
    return call


def _drop():
    return httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def test_zero_output_stale_kill_hands_off_to_fallback():
    call = _call()
    assert call._handle_stream_error(_drop(), 0, 2) is False
    err = call.result["error"]
    assert isinstance(err, StaleStreamNoOutputError)
    assert "300s" in str(err)
    assert isinstance(err.__cause__, httpx.RemoteProtocolError)


def test_last_attempt_also_raises_stale_error():
    call = _call()
    assert call._handle_stream_error(_drop(), 2, 2) is False
    assert isinstance(call.result["error"], StaleStreamNoOutputError)


@pytest.mark.parametrize("kwargs", [
    {"pending_fallback": False},  # nothing to fail over to: keep retrying
    {"stale_killed": False},      # ordinary disconnect, not a stale kill
])
def test_retries_same_model_when_fast_fallback_does_not_apply(kwargs):
    call = _call(**kwargs)
    call._retry_after_drop = MagicMock()
    assert call._handle_stream_error(_drop(), 0, 2) is True
    call._retry_after_drop.assert_called_once()
    assert call.result["error"] is None


def test_stale_kill_of_an_earlier_attempt_does_not_leak():
    call = _call()
    call.stream_attempt_state["current"] = 2  # attempt 1 was stale-killed, attempt 2 dropped normally
    call._retry_after_drop = MagicMock()
    assert call._handle_stream_error(_drop(), 1, 2) is True


def test_env_switch_restores_same_model_retries(monkeypatch):
    monkeypatch.setenv("HERMES_STALE_FAST_FALLBACK", "0")
    call = _call()
    call._retry_after_drop = MagicMock()
    assert call._handle_stream_error(_drop(), 0, 2) is True


def test_kill_stale_stream_records_the_attempt():
    call = _call(stale_killed=False)
    call.stream_attempt_state["current"] = 3
    call.clients = MagicMock()
    call._kill_stale_stream(301.0)
    assert call.stale_killed_attempt["id"] == 3


@pytest.mark.parametrize("approx_tokens,num_messages", [(1_000, 4), (180_000, 400)])
def test_classified_as_timeout_not_context_overflow(approx_tokens, num_messages):
    err = StaleStreamNoOutputError("No output from provider within the 300s stale timeout")
    err.__cause__ = _drop()
    classified = classify_api_error(
        err, provider="custom", model="kimi-latest", approx_tokens=approx_tokens,
        context_length=200_000, num_messages=num_messages)
    assert classified.reason == FailoverReason.timeout
    assert not classified.should_compress


def test_turn_recovery_fails_over_on_stale_error_without_retry_budget():
    import inspect

    import agent.turn_recovery as tr

    src = inspect.getsource(tr)
    assert "isinstance(api_error, StaleStreamNoOutputError)" in src
    assert tr.StaleStreamNoOutputError is StaleStreamNoOutputError
