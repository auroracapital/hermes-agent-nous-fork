#!/usr/bin/env python3
"""Slack egress approval broker. A separate process, a separate identity.

The caller (the agent process) never writes a grant. It talks to this broker
over a Unix socket. The broker checks the peer with SO_PEERCRED and accepts a
grant only from a uid that is NOT the caller's uid. The grants database and
the stop marker live where only the broker's uid can write them.

Default is deny. If the broker is down, the socket is missing, or the peer
check fails, nothing is granted and nothing may be sent.

This process holds no Slack credentials and opens no network connection.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path


def fingerprint(account: str, channel: str, text: str, attachment: str) -> str:
    raw = "\n".join([account or "", channel or "", text or "", attachment or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


READ_ALLOW = frozenset({
    "conversations_history",
    "conversations_replies",
    "conversations_search_messages",
    "conversations_unreads",
    "channels_list",
})

WRITE_KNOWN = frozenset({
    "chat.postMessage",
    "chat.update",
    "files_upload_v2",
    "reactions_add",
    "reactions_remove",
})


def _state_dir() -> Path:
    override = os.environ.get("SLACK_EGRESS_BROKER_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "state" / "slack-egress-broker"


def _private_dir(state_dir: Path) -> Path:
    """The database and the stop marker. Never the socket directory."""
    override = os.environ.get("SLACK_EGRESS_BROKER_PRIVATE")
    if override:
        return Path(override)
    return state_dir / "private"


class Broker:
    def __init__(self, state_dir: Path, caller_uid: int):
        self.state_dir = state_dir
        self.caller_uid = int(caller_uid)
        private = _private_dir(state_dir)
        self.db_path = private / "grants.sqlite"
        self.marker_path = private / "stop-marker"
        self.socket_path = state_dir / "broker.sock"

    def open(self) -> bool:
        try:
            text = self.marker_path.read_text(encoding="utf-8")
        except OSError:
            return False
        return text.strip() == "open"

    def _connect(self) -> sqlite3.Connection:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS grants (
                fp TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                channel TEXT NOT NULL,
                body TEXT NOT NULL,
                attachment TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            )"""
        )
        return con

    def grant(self, account: str, channel: str, text: str, attachment: str) -> str:
        fp = fingerprint(account, channel, text, attachment or "")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT OR IGNORE INTO grants (fp, account, channel, body, attachment) "
                "VALUES (?,?,?,?,?)",
                (fp, account, channel, text, attachment or ""),
            )
            con.execute("COMMIT")
        finally:
            con.close()
        return fp

    def claim(self, account: str, channel: str, text: str, attachment: str, tool_name: str) -> str:
        name = (tool_name or "").strip()
        if name in READ_ALLOW:
            return "read"
        if name not in WRITE_KNOWN:
            raise PermissionError(f"unknown tool {name}")
        if not self.open():
            raise PermissionError("stop marker missing, unreadable, empty or not open")
        if not account or not channel or text is None:
            raise PermissionError("account, channel and text are required")
        fp = fingerprint(account, channel, text, attachment or "")
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT consumed FROM grants WHERE fp=?", (fp,)).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                raise PermissionError("no pre-recorded grant for this exact text")
            if row[0]:
                con.execute("ROLLBACK")
                raise PermissionError("grant already consumed")
            cur = con.execute(
                "UPDATE grants SET consumed=1 WHERE fp=? AND consumed=0", (fp,)
            )
            if cur.rowcount != 1:
                con.execute("ROLLBACK")
                raise PermissionError("grant lost the race")
            con.execute("COMMIT")
        except PermissionError:
            raise
        finally:
            con.close()
        return fp

    def handle(self, peer_uid: int, request: dict) -> dict:
        op = str(request.get("op") or "")
        if op == "grant":
            if int(peer_uid) == self.caller_uid:
                return {"ok": False, "error": "caller uid cannot grant"}
            fp = self.grant(
                str(request.get("account") or ""),
                str(request.get("channel") or ""),
                str(request.get("text") or ""),
                str(request.get("attachment") or ""),
            )
            return {"ok": True, "fp": fp}
        if op == "claim":
            if int(peer_uid) != self.caller_uid:
                return {"ok": False, "error": "only the caller uid may claim"}
            try:
                fp = self.claim(
                    str(request.get("account") or ""),
                    str(request.get("channel") or ""),
                    str(request.get("text") or ""),
                    str(request.get("attachment") or ""),
                    str(request.get("tool") or ""),
                )
            except Exception as exc:  # noqa: BLE001 — every refusal is a deny
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "fp": fp}
        if op == "ping":
            return {"ok": True, "open": self.open()}
        return {"ok": False, "error": f"unknown op {op}"}


def serve(broker: Broker) -> None:
    broker.state_dir.mkdir(parents=True, exist_ok=True)
    if broker.socket_path.exists():
        broker.socket_path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(broker.socket_path))
    # Reachable by the caller. The database and the stop marker stay inside
    # state_dir, which is mode 0700 and owned by this process only.
    os.chmod(broker.socket_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP
             | stat.S_IROTH | stat.S_IWOTH)
    sock.listen(16)
    while True:
        conn, _ = sock.accept()
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            peer_pid, peer_uid, _peer_gid = _unpack_ucred(creds)
            del peer_pid
            raw = b""
            while b"\n" not in raw and len(raw) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
            try:
                request = json.loads(raw.decode("utf-8").strip() or "{}")
            except json.JSONDecodeError:
                request = {}
            reply = broker.handle(peer_uid, request)
            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        finally:
            conn.close()


def _unpack_ucred(creds: bytes):
    import struct

    return struct.unpack("3i", creds[:12])


def main() -> int:
    caller = os.environ.get("SLACK_EGRESS_CALLER_UID")
    if not caller or not caller.isdigit():
        sys.stderr.write("broker refuses to start: SLACK_EGRESS_CALLER_UID is not set\n")
        return 2
    broker = Broker(_state_dir(), int(caller))
    serve(broker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
