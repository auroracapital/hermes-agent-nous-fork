"""Explicit one-use approvals for mocked Slack clients, never a live broker.

Only the transport's OS peer lookup is simulated. Broker.handle still checks
caller/granter identities and consumes exact grants through the real database.
"""
import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from plugins.platforms.slack.egress_broker import Broker


@pytest.fixture
def slack_egress_broker(monkeypatch):
    caller = os.getuid()
    granter = caller + 1
    # Explicit /tmp avoids long pytest/HERMES_HOME paths and the AF_UNIX limit.
    with tempfile.TemporaryDirectory(prefix="seg-", dir="/tmp") as directory:
        root = Path(directory)
        monkeypatch.setenv("SLACK_EGRESS_BROKER_PRIVATE", str(root / "private"))
        broker = Broker(root, caller)
        broker.marker_path.parent.mkdir()
        broker.marker_path.write_text("open", encoding="utf-8")
        monkeypatch.setenv("SLACK_EGRESS_BROKER_SOCK", str(broker.socket_path))
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(broker.socket_path))
        server.listen()
        server.settimeout(0.05)
        stopped = threading.Event()

        def serve():
            while not stopped.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                with conn:
                    conn.settimeout(2)
                    raw = b""
                    while b"\n" not in raw:
                        part = conn.recv(65536)
                        if not part:
                            break
                        raw += part
                    request = json.loads(raw)
                    reply = broker.handle(caller, request)
                    conn.sendall((json.dumps(reply) + "\n").encode())

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        class Approvals:
            def grant(self, channel, text, attachment=""):
                reply = broker.handle(granter, {
                    "op": "grant", "account": channel, "channel": channel,
                    "text": text, "attachment": attachment,
                })
                assert reply["ok"], reply

            def caller_grant(self, channel, text):
                return broker.handle(caller, {
                    "op": "grant", "account": channel, "channel": channel,
                    "text": text,
                })

        try:
            yield Approvals()
        finally:
            stopped.set()
            thread.join(timeout=3)
            server.close()
            assert not thread.is_alive()
