"""Boot recovery of many local receipts must not freeze the gateway loop.

A cold start used to read every ``gateway.local_policy.v1`` receipt on the event loop,
one SQLite query per receipt. With ~15k open receipts that outran the loop-liveness
watchdog (probe timeout 10s, 3 strikes, exit 75) after "Gateway running". Recovery
still restores every boot receipt, in ``local_receipt_ids`` order, and yields between
batches so a probe scheduled the way the watchdog schedules one runs before the scan
finishes.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from gateway.shutdown_watchdog import DEFAULT_LOOP_WATCHDOG_TIMEOUT_S

RECEIPT_COUNT = 15_000


async def _authority(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run, "_load_gateway_config", lambda: {"platform_toolsets": {"cli": []}})
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner = SimpleNamespace(
        session_store=store, _session_db=store._db, adapters={}, _draining=False,
        config=SimpleNamespace(multiplex_profiles=False))
    authority = await initialize_session_authority(
        runner, profile_id=str(tmp_path), instance_id="owner")
    return authority


def _seed_open_receipts(authority, count):
    """Insert *count* open local receipts, cloning one real create so the checks pass."""
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from gateway.session_local_recovery import local_source
    from hermes_state_keys import local_identity
    from hermes_state_local import POLICY_PREFIX, local_receipt

    actor = AuthorityConnection(
        authority, SimpleNamespace(write=lambda frame: None), {"user_id": "owner"}).actor
    home = str(authority.db.db_path.parent)
    first = create_local_session(authority, actor, {
        "request_id": "bulk-0", "source": "gui", "cwd": home, "model": "m", "toolsets": []})
    template = local_receipt(authority.db, first.session_id)
    with authority.db._read_ctx() as conn:
        session = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (first.session_id,)).fetchone())
        routing = dict(conn.execute(
            "SELECT * FROM gateway_routing WHERE session_key=?", (template["route"],)).fetchone())
    profile_id, principal = template["profile_id"], template["principal_id"]
    store = authority.runner.session_store
    ids = [first.session_id]
    session_rows, routing_rows, meta_rows = [], [], []
    for index in range(1, count):
        request_id = f"bulk-{index}"
        sid = local_identity(profile_id, principal, request_id)
        source = local_source(authority, sid, principal)
        route = store._generate_session_key(source)
        origin = source.to_dict()
        entry = {**template["entry"], "session_id": sid, "session_key": route, "origin": origin}
        receipt = {
            **template, "request_id": request_id, "session_id": sid, "route": route,
            "entry": entry, "lineage": [sid]}
        session_rows.append((
            sid, session["source"], session["created_source"], principal, route, sid,
            session["model"], session["cwd"], session["profile_name"], json.dumps(origin),
            session["started_at"]))
        routing_rows.append((route, json.dumps(entry), routing["updated_at"]))
        meta_rows.append((POLICY_PREFIX + sid, json.dumps(receipt)))
        ids.append(sid)

    def write(conn):
        conn.executemany(
            "INSERT INTO sessions(id,source,created_source,user_id,session_key,chat_id,chat_type,"
            "model,cwd,profile_name,origin_json,started_at) VALUES(?,?,?,?,?,?,'dm',?,?,?,?,?)",
            session_rows)
        conn.executemany(
            "INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) VALUES('',?,?,?)",
            routing_rows)
        conn.executemany("INSERT INTO state_meta(key,value) VALUES(?,?)", meta_rows)

    authority.db._execute_write(write)
    authority.sessions.clear()
    return ids


@pytest.mark.asyncio
async def test_many_local_receipts_recover_without_stalling_the_loop(tmp_path, monkeypatch, caplog):
    from gateway.session_local_recovery import local_receipt_ids, recover_local_sessions_async
    import gateway.session_viewer_close as viewer_close

    # Startup arms orphan closes after the restore. Grace 0 skips the per-receipt
    # idle read; the sessions themselves are still restored either way.
    monkeypatch.setattr(viewer_close, "orphan_close_grace", lambda authority, startup=False: 0)
    authority = await _authority(tmp_path, monkeypatch)
    seeded = _seed_open_receipts(authority, RECEIPT_COUNT)
    expected = local_receipt_ids(authority)
    assert len(expected) == RECEIPT_COUNT
    assert set(expected) == set(seeded)
    assert authority.sessions == {}

    caplog.set_level(logging.INFO, logger="gateway.session_local_recovery")
    loop = asyncio.get_running_loop()
    probe = asyncio.Event()
    seen = {}

    def mark():
        restored = len(authority.sessions)
        if 0 < restored < RECEIPT_COUNT:
            seen["restored"] = restored
            probe.set()
            return
        if not task.done():
            loop.call_soon(mark)

    task = asyncio.create_task(recover_local_sessions_async(authority, schedule=True))
    loop.call_soon(mark)
    await asyncio.wait_for(probe.wait(), timeout=DEFAULT_LOOP_WATCHDOG_TIMEOUT_S)
    assert not task.done()
    assert 0 < seen["restored"] < RECEIPT_COUNT

    await task
    assert list(authority.sessions) == expected
    progress = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Local session recovery ")
        and not record.getMessage().startswith("Local session recovery finished")]
    finished = [
        record.getMessage() for record in caplog.records
        if record.getMessage().startswith("Local session recovery finished")]
    assert progress and finished
    assert any(not message.startswith(f"Local session recovery {RECEIPT_COUNT}/") for message in progress)
    assert str(RECEIPT_COUNT) in finished[-1]
