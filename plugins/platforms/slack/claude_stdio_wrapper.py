#!/usr/bin/env python3
"""Wrap the Claude Slack stdio server. Reads pass, every other name denies.

The binary schema is unknown, so this wrapper does not guess write names.
It answers an initialize and a tools/list itself, and only forwards a
tools/call whose name is on the read allowlist. Anything else is a JSON-RPC
error and never reaches the binary. No network, no credentials.
"""
from __future__ import annotations

import json
import sys

READ_ALLOW = frozenset({
    "conversations_history",
    "conversations_replies",
    "conversations_search_messages",
    "conversations_unreads",
    "channels_list",
})


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}}, "serverInfo": {"name": "slack-egress-wrapper", "version": "0"}}}
    if method == "tools/list":
        tools = [{"name": n, "description": "read"} for n in sorted(READ_ALLOW)]
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        name = str((msg.get("params") or {}).get("name") or "")
        if name not in READ_ALLOW:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32600,
                    "message": f"denied: {name} is not on the read allowlist"}}
        return None  # caller forwards this one
    if method and method.startswith("notifications/"):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32600, "message": "denied: notification"}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"denied: {method}"}}


def main() -> int:
    raw = sys.stdin.readline()
    msg = json.loads(raw or "{}")
    out = handle(msg)
    if out is None:
        sys.stdout.write(json.dumps({"forward": True}) + "\n")
    else:
        sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
