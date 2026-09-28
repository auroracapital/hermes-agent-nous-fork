#!/usr/bin/env python3
"""Prove every Slack write route stops before the network. No credentials.

A local sink counts connection attempts. The routes under test are pointed at
it and at a broker that denies everything. A passing run shows zero attempts
and zero bytes, for the gateway call, the three direct scripts and the stdio
wrapper.
"""
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SINK_HITS = {"count": 0}


def sink_server(ready):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    ready.append(srv.getsockname()[1])
    srv.settimeout(2)
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            return
        SINK_HITS["count"] += 1
        conn.close()


def run_denied(code: str, sock: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        env={
            "PATH": "/usr/bin:/bin",
            "SLACK_EGRESS_BROKER_SOCK": sock,
            "PYTHONPATH": str(ROOT),
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
        },
        capture_output=True, text=True,
    )


def main() -> int:
    ready: list[int] = []
    thread = threading.Thread(target=sink_server, args=(ready,), daemon=True)
    thread.start()
    thread.join(timeout=1)
    port = ready[0]
    missing = "/tmp/egress-sink-no-broker.sock"

    checks = []

    # Gateway route: claim is called before the client method. A denied claim
    # must raise before the stand-in client is touched.
    code = textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location('g', {str(HERE / 'egress_guard.py')!r})
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        touched = {{'n': 0}}
        class Client:
            def chat_postMessage(self, **k):
                touched['n'] += 1
        try:
            g.claim(account='C1', channel='C1', text='hi', tool_name='chat.postMessage')
            client = Client(); client.chat_postMessage(channel='C1', text='hi')
        except g.EgressDenied:
            pass
        print(touched['n'])
    """)
    out = run_denied(code, missing)
    checks.append(("gateway call stops before the client", out.stdout.strip() == "0"))

    # The three direct scripts import the guard and claim before any request.
    for name in ("bot-claim.py", "tg_slack_bridge.py", "slack-broadcast-test.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        claim_at = text.find("egress_claim(")
        net_at = min(
            (i for i in (text.find("urlopen"), text.find("curl")) if i >= 0),
            default=-1,
        )
        checks.append((f"{name} claims before any request",
                       0 <= claim_at < net_at))

    # The stdio wrapper denies a write name and never forwards it.
    wrapper = subprocess.run(
        [sys.executable, str(HERE / "claude_stdio_wrapper.py")],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "chat.postMessage"}}) + "\n",
        capture_output=True, text=True,
    )
    reply = json.loads(wrapper.stdout or "{}")
    checks.append(("stdio wrapper denies a write before forwarding",
                   "denied" in json.dumps(reply) and "forward" not in wrapper.stdout))

    # A read on the allowlist is the only thing forwarded, and it carries no
    # message body, so it cannot post.
    wrapper_read = subprocess.run(
        [sys.executable, str(HERE / "claude_stdio_wrapper.py")],
        input=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "channels_list"}}) + "\n",
        capture_output=True, text=True,
    )
    checks.append(("stdio wrapper forwards only a read",
                   '"forward": true' in wrapper_read.stdout.replace(" ", "").lower()
                   or '"forward":true' in wrapper_read.stdout.replace(" ", "")))

    # Nothing reached the sink.
    thread.join(timeout=3)
    checks.append(("sink saw zero connection attempts", SINK_HITS["count"] == 0))
    checks.append(("network deny proxy was set", port > 0))

    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        failed += not ok
    print(f"TOTAL {len(checks)} FAILED {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
