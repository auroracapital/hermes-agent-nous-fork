#!/usr/bin/env python3
"""Run the real Slack write routes against a denying broker and a counting sink.

No credentials, no real Slack host. Each route is executed. The sink records
every connection attempt. A pass means the route returned a refusal and the
sink stayed at zero.
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
VENV = "/home/ubuntu/.hermes/hermes-agent/venv/bin/python"
PY = VENV if Path(VENV).exists() else sys.executable
HITS = {"n": 0}
PORT = {"n": 0}


def serve(ready):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)
    ready.append(srv.getsockname()[1])
    srv.settimeout(0.5)
    while not ready[1:]:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        HITS["n"] += 1
        try:
            conn.recv(65536)
        except OSError:
            pass
        conn.close()
    srv.close()


def run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-c", code],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
            "SLACK_EGRESS_BROKER_SOCK": "/tmp/egress-route-no-broker.sock",
            "https_proxy": f"http://127.0.0.1:{PORT['n']}",
            "http_proxy": f"http://127.0.0.1:{PORT['n']}",
            "HTTPS_PROXY": f"http://127.0.0.1:{PORT['n']}",
            "ALL_PROXY": f"http://127.0.0.1:{PORT['n']}",
            "SLACK_BOT_TOKEN": "xoxb-test-not-real",
            "SLACK_EGRESS_APPROVED": "1",
        },
        capture_output=True, text=True, timeout=60,
    )


def main() -> int:
    ready: list = []
    threading.Thread(target=serve, args=(ready,), daemon=True).start()
    for _ in range(50):
        if ready:
            break
        threading.Event().wait(0.02)
    PORT["n"] = ready[0]
    checks = []

    bridge = run(textwrap.dedent(f"""
        import importlib.util, os, sys
        os.environ['HERMES_HOME'] = '/tmp/egress-route-home'
        spec = importlib.util.spec_from_file_location(
            'bridge', {str(HERE / 'tg_slack_bridge.py')!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        out = m.SlackClient('xoxb-test-not-real').post('C1', 'hello from the route test')
        print(json_ok(out) if False else __import__('json').dumps(out))
    """))
    checks.append(("bridge post is refused",
                   bridge.returncode == 0 and "egress denied" in bridge.stdout))

    claim = run(textwrap.dedent(f"""
        import importlib.util, os
        os.environ['HERMES_HOME'] = '/tmp/egress-route-home'
        spec = importlib.util.spec_from_file_location(
            'botclaim', {str(HERE / 'bot-claim.py')!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        ok, detail = m.naar_slack('hello from the route test')
        print('ok=' + str(ok))
        print(detail)
    """))
    checks.append(("bot-claim send is refused",
                   claim.returncode == 0 and "ok=False" in claim.stdout
                   and "geweigerd" in claim.stdout))

    wrapper = subprocess.run(
        [PY, str(HERE / "claude_stdio_wrapper.py")],
        input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "chat.postMessage",
                                     "arguments": {"channel": "C1", "text": "hi"}}}) + "\n",
        capture_output=True, text=True, timeout=30,
    )
    checks.append(("stdio wrapper refuses a write",
                   wrapper.returncode == 0 and "denied" in wrapper.stdout
                   and "forward" not in wrapper.stdout))

    broadcast = run(textwrap.dedent(f"""
        import runpy, sys
        sys.argv = ['broadcast', 'channel']
        try:
            runpy.run_path({str(HERE / 'slack-broadcast-test.py')!r}, run_name='__main__')
        except SystemExit as e:
            print('exit=' + str(e.code))
    """))
    checks.append(("broadcast stops before curl",
                   "geweigerd" in broadcast.stdout or "geweigerd" in broadcast.stderr))

    ready.append("stop")
    checks.append(("sink saw zero attempts", HITS["n"] == 0))

    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
            detail = {"bridge": bridge, "claim": claim, "wrapper": wrapper,
                      "broadcast": broadcast}.get(name.split()[0].rstrip("-"))
    if failed:
        print("--- bridge stdout ---"); print(bridge.stdout[-400:])
        print("--- bridge stderr ---"); print(bridge.stderr[-400:])
        print("--- claim stdout ---"); print(claim.stdout[-400:])
        print("--- claim stderr ---"); print(claim.stderr[-400:])
        print("--- broadcast stdout ---"); print(broadcast.stdout[-400:])
        print("--- broadcast stderr ---"); print(broadcast.stderr[-400:])
    print(f"TOTAL {len(checks)} FAILED {failed} SINK {HITS['n']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
