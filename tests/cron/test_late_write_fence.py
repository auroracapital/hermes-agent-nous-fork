"""A cron cleanup timeout must not let the timed-out write commit afterwards.

The cleanup bound abandons the call, but the worker thread keeps running. Without a
fence that thread still commits end_session after the timeout, and a later close()
reopens the handle so the late stamp lands. A normal finish still stamps.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from cron.scheduler import _BoundedCronSessionDB
from hermes_state import AsyncSessionDB, SessionDB


class Sub(SessionDB):
    """A real SessionDB subclass, the shape a registry wrapper has."""


class _Gate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()


def _cold(path, sid):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT ended_at, end_reason FROM sessions WHERE id=?", (sid,)
        ).fetchone()


def _hold_end_session(db, gate):
    original = db._execute_write

    def gated(fn, patience_s=None):
        name = getattr(fn, "__qualname__", "")
        holds = name.endswith("end_session.<locals>.<lambda>")

        def wrapped(conn):
            result = fn(conn)
            if holds:
                gate.entered.set()
                gate.release.wait()
            return result

        try:
            return original(wrapped, patience_s=patience_s)
        finally:
            if holds:
                gate.returned.set()

    db._execute_write = gated


def _make(tmp_path, cls, name):
    path = tmp_path / f"{name}.db"
    db = cls(db_path=path)
    sid = f"cron_{name}"
    db.create_session(sid, "cron")
    db.append_message(sid, "assistant", "finished reply")
    return path, db, sid


def _run_late(tmp_path, monkeypatch, cls, name, wrap):
    path, db, sid = _make(tmp_path, cls, name)
    gate = _Gate()
    _hold_end_session(db, gate)
    monkeypatch.setattr("cron.scheduler._cron_cleanup_timeout_seconds", lambda: 0.05)
    bounded = _BoundedCronSessionDB(wrap(db), name)
    try:
        with pytest.raises(TimeoutError, match="end_session"):
            bounded.end_session(sid, "cron_complete")
        assert gate.entered.wait(2)
        assert _cold(path, sid) == (None, None)
    finally:
        gate.release.set()
    assert gate.returned.wait(5)
    threading.Event().wait(0.3)
    db.close()
    return path, db, sid


@pytest.mark.parametrize(
    "cls,wrap",
    [(Sub, lambda d: d), (SessionDB, AsyncSessionDB), (Sub, AsyncSessionDB)],
)
def test_late_end_session_never_commits(tmp_path, monkeypatch, cls, wrap):
    path, db, sid = _run_late(
        tmp_path, monkeypatch, cls, f"late_{cls.__name__}_{wrap is AsyncSessionDB}", wrap
    )
    ended, reason = _cold(path, sid)
    assert reason != "cron_complete"
    assert ended is None
    assert db.append_message(sid, "assistant", "unrelated later") > 0
    db.close()


def test_other_thread_write_lands_while_fence_is_cancelled(tmp_path, monkeypatch):
    path, db, sid = _make(tmp_path, Sub, "other_thread")
    gate = _Gate()
    _hold_end_session(db, gate)
    monkeypatch.setattr("cron.scheduler._cron_cleanup_timeout_seconds", lambda: 0.05)
    bounded = _BoundedCronSessionDB(db, "other")
    landed = []
    try:
        with pytest.raises(TimeoutError):
            bounded.end_session(sid, "cron_complete")
        assert gate.entered.wait(2)
        worker = threading.Thread(
            target=lambda: landed.append(db.append_message(sid, "assistant", "other"))
        )
        worker.start()
        assert not worker.join(0.2)
    finally:
        gate.release.set()
    worker.join(5)
    assert landed and landed[0] > 0
    assert gate.returned.wait(5)
    db.close()
    assert _cold(path, sid)[1] != "cron_complete"


def test_timeout_fails_later_calls_with_the_same_error(tmp_path, monkeypatch):
    path, db, sid = _make(tmp_path, Sub, "primary")
    gate = _Gate()
    _hold_end_session(db, gate)
    monkeypatch.setattr("cron.scheduler._cron_cleanup_timeout_seconds", lambda: 0.05)
    bounded = _BoundedCronSessionDB(db, "primary")
    try:
        with pytest.raises(TimeoutError) as first:
            bounded.end_session(sid, "cron_complete")
        calls = []
        for name in ("end_session", "close"):
            monkeypatch.setattr(db, name, lambda *a, _n=name, **k: calls.append(_n))
            args = (sid, "cron_complete") if name == "end_session" else ()
            with pytest.raises(TimeoutError) as again:
                getattr(bounded, name)(*args)
            assert again.value is first.value
        assert calls == []
    finally:
        gate.release.set()
    assert gate.returned.wait(5)
    db.close()
    assert _cold(path, sid) == (None, None)


def test_async_store_without_sync_core_is_rejected():
    called = []

    class AsyncOnly:
        async def end_session(self, sid, reason):
            called.append((sid, reason))

    bounded = _BoundedCronSessionDB(AsyncOnly(), "async-reject")
    with pytest.raises(TypeError, match="synchronous"):
        bounded.end_session("s", "cron_complete")
    assert called == []


def test_async_facade_unwraps_to_the_inner_store(tmp_path):
    path, db, sid = _make(tmp_path, SessionDB, "unwrap")
    bounded = _BoundedCronSessionDB(AsyncSessionDB(db), "unwrap")
    assert bounded._session_db is db
    bounded.end_session(sid, "cron_complete")
    assert _cold(path, sid)[1] == "cron_complete"
    db.close()


def test_ordinary_finish_still_stamps(tmp_path):
    path, db, sid = _make(tmp_path, Sub, "ordinary")
    bounded = _BoundedCronSessionDB(db, "ordinary")
    bounded.end_session(sid, "cron_complete")
    ended, reason = _cold(path, sid)
    assert reason == "cron_complete" and ended is not None
    db.close()
