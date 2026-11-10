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
from pathlib import Path
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


def _runner_for(authority):
    return SimpleNamespace(
        session_store=authority.runner.session_store,
        _session_db=authority.runner._session_db,
        adapters={},
        _draining=False,
        config=SimpleNamespace(multiplex_profiles=False),
    )


async def _worst_block(factory):
    """Longest stretch the loop went without running a ``call_soon`` while *factory* ran."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(factory())
    gaps = []
    last = loop.time()

    def beat():
        nonlocal last
        now = loop.time()
        gaps.append(now - last)
        last = now
        if not task.done():
            loop.call_soon(beat)

    loop.call_soon(beat)
    result = await task
    return (max(gaps) if gaps else 0.0), result


@pytest.mark.asyncio
async def test_a_receipt_closed_or_compressed_between_batches_is_not_resurrected(tmp_path, monkeypatch):
    """A yield between batches must re-read. A stale prefetch would put the closed session
    back and would restore the pre-compression transcript."""
    import gateway.session_local_recovery as recovery
    from hermes_state_local import POLICY_PREFIX, local_receipt

    monkeypatch.setattr(recovery, "_RECOVERY_BATCH", 1)
    authority = await _authority(tmp_path, monkeypatch)
    _seed_open_receipts(authority, 4)
    order = recovery.local_receipt_ids(authority)
    authority.sessions.clear()
    closed_id, compressed_id = order[1], order[2]
    child_id = compressed_id + "-c"
    route = local_receipt(authority.db, compressed_id)["route"]
    mutated = {"done": False}

    def _close_and_compress():
        def write(conn):
            conn.execute("DELETE FROM state_meta WHERE key=?", (POLICY_PREFIX + closed_id,))
            conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (1_700_000_000.0, "ws_orphan_reap", closed_id),
            )
            parent = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (compressed_id,)).fetchone())
            conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason='compression' WHERE id=?",
                (1_700_000_000.0, compressed_id),
            )
            parent["id"] = child_id
            parent["parent_session_id"] = compressed_id
            parent["ended_at"] = None
            parent["end_reason"] = None
            parent["model_config"] = "{}"
            columns = list(parent)
            conn.execute(
                f"INSERT INTO sessions ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                [parent[column] for column in columns],
            )
            receipt = json.loads(conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (POLICY_PREFIX + compressed_id,)).fetchone()[0])
            receipt["lineage"] = [compressed_id, child_id]
            receipt["entry"] = {**receipt["entry"], "session_id": child_id}
            conn.execute(
                "UPDATE state_meta SET value=? WHERE key=?",
                (json.dumps(receipt), POLICY_PREFIX + compressed_id),
            )
            conn.execute("DELETE FROM gateway_routing WHERE session_key=?", (route,))
        authority.db._execute_write(write)
        authority.runner.session_store._entries.pop(route, None)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(recovery.recover_local_sessions_async(authority))

    def mark():
        if (not mutated["done"] and not task.done() and len(authority.sessions) >= 1
                and closed_id not in authority.sessions and compressed_id not in authority.sessions):
            mutated["done"] = True
            _close_and_compress()
        if not task.done():
            loop.call_soon(mark)

    loop.call_soon(mark)
    await task
    assert mutated["done"]
    assert closed_id not in authority.sessions
    assert authority.runner.session_store._entries[route].session_id == child_id


@pytest.mark.asyncio
async def test_boot_and_hot_serve_of_15k_receipts_answer_a_probe_within_the_watchdog_timeout(
        tmp_path, monkeypatch):
    """Seed before ``initialize_session_authority``, which is the cold-boot path and the
    hot-serve path (``register=False``, what ``serve_profile_runtime`` builds)."""
    from gateway.session_authority import initialize_session_authority

    authority = await _authority(tmp_path, monkeypatch)
    _seed_open_receipts(authority, RECEIPT_COUNT)
    profile_id = str(tmp_path)

    async def boot():
        return await initialize_session_authority(
            _runner_for(authority), profile_id=profile_id, instance_id="boot", register=True)

    async def hot():
        return await initialize_session_authority(
            _runner_for(authority), profile_id=profile_id, instance_id="hot", register=False)

    boot_gap, booted = await _worst_block(boot)
    hot_gap, served = await _worst_block(hot)
    assert len(booted.sessions) == RECEIPT_COUNT
    assert len(served.sessions) == RECEIPT_COUNT
    assert boot_gap < DEFAULT_LOOP_WATCHDOG_TIMEOUT_S
    assert hot_gap < DEFAULT_LOOP_WATCHDOG_TIMEOUT_S
    Path("/tmp/recovery-loop-blocks.txt").write_text(
        f"boot {boot_gap:.4f}\nhot {hot_gap:.4f}\n", encoding="utf-8")
