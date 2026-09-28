"""Slack egress guard. One claim before any network effect.

Fail closed. A missing, unreadable, empty or non-open stop marker denies.
An exact approved row (account, channel, text, attachment) is required
before the insert. A unique key plus BEGIN IMMEDIATE makes two processes
race to one insert. A crash after the effect leaves the row uncertain, and
uncertain never replays by itself.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

READ_ALLOW = frozenset({
    "conversations_history",
    "conversations_replies",
    "conversations_search_messages",
    "conversations_unreads",
    "channels_list",
})


class EgressDenied(Exception):
    """Raised when a Slack write must not reach the network."""


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def db_path() -> Path:
    override = os.environ.get("SLACK_EGRESS_DB")
    if override:
        return Path(override)
    return _home() / "state" / "slack-egress.sqlite"


def marker_path() -> Path:
    override = os.environ.get("SLACK_ESTOP_PATH")
    if override:
        return Path(override)
    return _home() / "state" / "slack-estop"


def marker_open() -> bool:
    """Only the exact word 'open' allows a claim. Anything else denies."""
    path = marker_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return text.strip() == "open"


def fingerprint(account: str, channel: str, text: str, attachment: str) -> str:
    raw = "\n".join([account or "", channel or "", text or "", attachment or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS claims (
            fp TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            channel TEXT NOT NULL,
            body TEXT NOT NULL,
            attachment TEXT NOT NULL,
            approved INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )"""
    )
    return con


WRITE_KNOWN = frozenset({
    "chat.postMessage",
    "chat.update",
    "files_upload_v2",
    "reactions_add",
    "reactions_remove",
})


def classify(tool_name: str) -> str:
    """Return 'read' or 'deny'. Only the read list passes; every other name denies."""
    name = (tool_name or "").strip()
    if name in READ_ALLOW:
        return "read"
    if name in WRITE_KNOWN:
        return "write"
    return "deny"


def claim(account: str, channel: str, text: str, attachment: str = "",
          approved: bool = False, tool_name: str = "chat.postMessage") -> str:
    """Insert one exclusive claim. Raises EgressDenied on every refusal.

    The caller must invoke this before the network call. A successful return
    is the only permission to send. The row stays 'claimed' until the caller
    reports the outcome.
    """
    if classify(tool_name) == "read":
        return "read"
    if classify(tool_name) != "write":
        raise EgressDenied(f"unknown tool {tool_name}")
    if not marker_open():
        raise EgressDenied("stop marker missing, unreadable, empty or not open")
    if not approved:
        raise EgressDenied("exact approval missing")
    if not account or not channel or text is None:
        raise EgressDenied("account, channel and text are required")
    fp = fingerprint(account, channel, text, attachment or "")
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT status FROM claims WHERE fp=?", (fp,)).fetchone()
        if row is not None:
            con.execute("ROLLBACK")
            raise EgressDenied(f"already {row[0]}")
        con.execute(
            "INSERT INTO claims (fp, account, channel, body, attachment, approved, status) "
            "VALUES (?,?,?,?,?,1,'claimed')",
            (fp, account, channel, text, attachment or ""),
        )
        con.execute("COMMIT")
    except EgressDenied:
        raise
    except sqlite3.Error as exc:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise EgressDenied(f"claim failed: {exc}") from exc
    finally:
        con.close()
    return fp


def mark(fp: str, status: str) -> None:
    """Record the outcome. 'uncertain' is terminal: it never auto-replays."""
    if status not in {"sent", "uncertain", "denied"}:
        raise EgressDenied(f"unknown status {status}")
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE claims SET status=? WHERE fp=? AND status='claimed'", (status, fp))
        con.execute("COMMIT")
    finally:
        con.close()
