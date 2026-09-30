"""Slack egress guard. The caller holds no authority to approve itself.

A write reaches the network only after a claim that a separate broker
process accepted. The broker runs as a different operating-system user,
owns the grants database and the stop marker, and checks the peer identity
of every request. This module never opens that database and never records
a grant.

Default is deny. No broker, a refused peer, a missing grant, a consumed
grant or a closed marker all raise EgressDenied before any network effect.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

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


class EgressDenied(Exception):
    """Raised when a Slack write must not reach the network."""


def broker_socket() -> Path:
    """Where the broker listens. The caller cannot choose the database.

    SLACK_EGRESS_DB and SLACK_ESTOP_PATH used to let the caller point the
    guard at a file it owns. Both are ignored on purpose. The socket path is
    the only locator, and it names a socket the broker owns, not a database.
    """
    override = os.environ.get("SLACK_EGRESS_BROKER_SOCK")
    if override:
        return Path(override)
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    return home / "state" / "slack-egress-broker" / "broker.sock"


def classify(tool_name: str) -> str:
    """Return 'read', 'write' or 'deny'. Only the read list passes locally."""
    name = (tool_name or "").strip()
    if name in READ_ALLOW:
        return "read"
    if name in WRITE_KNOWN:
        return "write"
    return "deny"


def _ask(request: dict) -> dict:
    path = broker_socket()
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        raw = b""
        while b"\n" not in raw and len(raw) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    except OSError as exc:
        raise EgressDenied(f"broker unreachable: {exc}") from exc
    finally:
        if sock is not None:
            sock.close()
    try:
        reply = json.loads(raw.decode("utf-8").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise EgressDenied("broker reply unreadable") from exc
    if not reply.get("ok"):
        raise EgressDenied(str(reply.get("error") or "broker denied"))
    return reply


def grant(account: str, channel: str, text: str, attachment: str = "") -> str:
    """Ask the broker to record one approval.

    The broker refuses this from the caller's own uid. A caller that imports
    and calls grant() gets a denial, never a usable approval.
    """
    reply = _ask({
        "op": "grant",
        "account": account,
        "channel": channel,
        "text": text,
        "attachment": attachment or "",
    })
    return str(reply.get("fp") or "")


def claim(account: str, channel: str, text: str, attachment: str = "",
          approved: bool = False, tool_name: str = "chat.postMessage") -> str:
    """Ask the broker to consume one exact grant. Raises EgressDenied otherwise.

    The approved flag is ignored. A read on the allowlist returns without
    touching the broker, because a read has no network effect to guard.
    """
    del approved
    kind = classify(tool_name)
    if kind == "read":
        return "read"
    if kind != "write":
        raise EgressDenied(f"unknown tool {tool_name}")
    reply = _ask({
        "op": "claim",
        "account": account,
        "channel": channel,
        "text": text,
        "attachment": attachment or "",
        "tool": tool_name,
    })
    return str(reply.get("fp") or "")


def mark(fp: str, status: str) -> None:
    """Kept for the adapter. The broker already consumed the grant at claim,
    so a crash after the effect cannot be replayed. Nothing here re-opens it."""
    del fp
    if status not in {"sent", "uncertain", "denied"}:
        raise EgressDenied(f"unknown status {status}")
