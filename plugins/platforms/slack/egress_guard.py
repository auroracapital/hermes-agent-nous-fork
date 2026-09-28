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


def grant(account: str, channel: str, text: str, attachment: str = "") -> str:
    """Record one exact approval before any send. Tests and a human gate call
    this. A caller of claim cannot."""
    fp = fingerprint(account, channel, text, attachment or "")
    con = _connect()
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

    The approved flag is ignored. Permission comes only from a pre-recorded
    grant for this exact account, channel, text and attachment, consumed once
    inside the same transaction as the insert. A caller cannot grant itself.
    """
    del approved  # caller-supplied; never trusted
    if classify(tool_name) == "read":
        return "read"
    if classify(tool_name) != "write":
        raise EgressDenied(f"unknown tool {tool_name}")
    if not marker_open():
        raise EgressDenied("stop marker missing, unreadable, empty or not open")
    if not account or not channel or text is None:
        raise EgressDenied("account, channel and text are required")
    fp = fingerprint(account, channel, text, attachment or "")
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        grant_row = con.execute(
            "SELECT consumed FROM grants WHERE fp=?", (fp,)
        ).fetchone()
        if grant_row is None:
            con.execute("ROLLBACK")
            raise EgressDenied("no pre-recorded grant for this exact text")
        if grant_row[0]:
            con.execute("ROLLBACK")
            raise EgressDenied("grant already consumed")
        existing = con.execute("SELECT status FROM claims WHERE fp=?", (fp,)).fetchone()
        if existing is not None:
            con.execute("ROLLBACK")
            raise EgressDenied(f"already {existing[0]}")
        cur = con.execute(
            "UPDATE grants SET consumed=1 WHERE fp=? AND consumed=0", (fp,)
        )
        if cur.rowcount != 1:
            con.execute("ROLLBACK")
            raise EgressDenied("grant lost the race")
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
