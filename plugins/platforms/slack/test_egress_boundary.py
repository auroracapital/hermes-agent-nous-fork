#!/usr/bin/env python3
"""Negative tests for the Slack egress boundary. No network, no credentials.

The broker runs as a DIFFERENT uid from the caller, inside a throwaway
directory this script creates and removes. Every refusal must happen before
any Slack call: the harness below records a send attempt and asserts it
stays at zero.
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
BROKER = HERE / "egress_broker.py"
RESULTS = []
SENDS = {"count": 0}


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ((" " + detail) if detail else ""))


def caller_uid() -> int:
    return os.getuid()


def other_uid() -> int:
    """A uid that is not ours and can own files. The default is the existing
    theme runner; override with SLACK_EGRESS_TEST_UID when it differs."""
    override = os.environ.get("SLACK_EGRESS_TEST_UID")
    if override:
        return int(override)
    import pwd
    return pwd.getpwnam("runner-theme").pw_uid


class BrokerProc:
    def __init__(self, uid: int):
        self.uid = uid
        self.gid = uid
        self.base = Path("/tmp") / f"egress-boundary-{os.getpid()}-{id(self)}"
        self.state = self.base / "state"
        self.sockdir = self.base / "sock"
        self.sock = self.sockdir / "broker.sock"
        self.proc = None

    def start(self):
        subprocess.run(
            ["sudo", "-n", "install", "-d", "-o", str(self.uid), "-g", str(self.gid),
             "-m", "711", str(self.base)],
            check=True,
        )
        subprocess.run(
            ["sudo", "-n", "install", "-d", "-o", str(self.uid), "-g", str(self.gid),
             "-m", "755", str(self.sockdir)],
            check=True,
        )
        subprocess.run(
            ["sudo", "-n", "install", "-d", "-o", str(self.uid), "-g", str(self.gid),
             "-m", "700", str(self.state)],
            check=True,
        )
        self.proc = subprocess.Popen(
            ["sudo", "-n", "-u", f"#{self.uid}", "-g", f"#{self.gid}",
             "env", f"SLACK_EGRESS_BROKER_DIR={self.sockdir}",
             f"SLACK_EGRESS_CALLER_UID={caller_uid()}",
             f"SLACK_EGRESS_BROKER_PRIVATE={self.state}",
             f"PYTHONPATH={ROOT}",
             sys.executable, str(BROKER)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        for _ in range(100):
            if self.sock.exists():
                return
            if self.proc.poll() is not None:
                err = b""
                if self.proc.stderr is not None:
                    err = self.proc.stderr.read()
                raise RuntimeError(f"broker exited early: {err.decode('utf-8', 'replace')}")
            time.sleep(0.05)
        raise RuntimeError("broker socket never appeared")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        subprocess.run(["sudo", "-n", "rm", "-rf", str(self.base)], check=False)

    def marker(self, word: str):
        path = self.state / "stop-marker"
        subprocess.run(
            ["sudo", "-n", "-u", f"#{self.uid}", "tee", str(path)],
            input=word.encode(), stdout=subprocess.DEVNULL, check=True,
        )

    def as_other(self, body: str):
        """Run body as the broker's uid, so it CAN grant. Returns stdout."""
        code = (
            "import importlib.util, os, sys\n"
            f"spec = importlib.util.spec_from_file_location('g', {str(HERE / 'egress_guard.py')!r})\n"
            "assert spec and spec.loader\n"
            "g = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(g)\n"
            + body
        )
        out = subprocess.run(
            ["sudo", "-n", "-u", f"#{self.uid}", "env",
             f"SLACK_EGRESS_BROKER_SOCK={self.sock}",
             sys.executable, "-c", code],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or out.stdout.strip())
        return out.stdout.strip()


def load_guard(sock: Path):
    os.environ["SLACK_EGRESS_BROKER_SOCK"] = str(sock)
    os.environ["SLACK_EGRESS_DB"] = "/tmp/attacker-owned-egress.sqlite"
    os.environ["SLACK_ESTOP_PATH"] = "/tmp/attacker-owned-marker"
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "egress_guard_under_test", HERE / "egress_guard.py")
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    return guard


def try_send(guard, **kw):
    """Stand-in for every Slack write route. Counts only what gets past claim."""
    try:
        guard.claim(**kw)
    except guard.EgressDenied:
        return False
    SENDS["count"] += 1
    return True


def main() -> int:
    broker = BrokerProc(other_uid())
    try:
        broker.start()
        guard = load_guard(broker.sock)

        # 1. Importing and calling grant() from the caller uid is refused.
        refused = False
        try:
            guard.grant("acct", "C1", "wire funds now")
        except guard.EgressDenied as exc:
            refused = "caller uid cannot grant" in str(exc)
        check("caller grant() is refused", refused)

        # 2. A caller-selected database path is ignored: writing a grant row
        #    into it with raw SQL never produces a claim.
        attacker_db = Path("/tmp/attacker-owned-egress.sqlite")
        attacker_db.unlink(missing_ok=True)
        con = sqlite3.connect(str(attacker_db))
        con.execute("CREATE TABLE grants (fp TEXT PRIMARY KEY, account TEXT, "
                    "channel TEXT, body TEXT, attachment TEXT, consumed INTEGER)")
        con.execute("INSERT INTO grants VALUES ('x','acct','C1','wire funds now','',0)")
        con.commit()
        con.close()
        sent = try_send(guard, account="acct", channel="C1", text="wire funds now",
                        approved=True, tool_name="chat.postMessage")
        check("caller-owned db via env does not grant", not sent)
        attacker_db.unlink(missing_ok=True)

        # 3. A real grant, made by the other uid, lets exactly one claim through.
        broker.marker("open")
        broker.as_other("print(g.grant('acct', 'C1', 'hello there'))")
        first = try_send(guard, account="acct", channel="C1", text="hello there",
                         tool_name="chat.postMessage")
        check("other-uid grant allows one claim", first)

        # 4. Replaying the same grant is refused. Stale and forged too.
        replay = try_send(guard, account="acct", channel="C1", text="hello there",
                          tool_name="chat.postMessage")
        check("replayed grant is refused", not replay)
        forged = try_send(guard, account="acct", channel="C1", text="forged text",
                          approved=True, tool_name="chat.postMessage")
        check("forged text is refused", not forged)

        # 5. Two claims at once: exactly one wins.
        broker.as_other("g.grant('acct', 'C1', 'race me')")
        winners = []

        def race():
            winners.append(try_send(
                guard, account="acct", channel="C1", text="race me",
                tool_name="chat.postMessage"))

        threads = [threading.Thread(target=race) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("concurrent claims admit exactly one", winners.count(True) == 1,
              f"winners={winners.count(True)}")

        # 6. Direct SQL into the broker's own database, as the caller, fails
        #    because the caller cannot write that file.
        sql_blocked = False
        try:
            sqlite3.connect(str(broker.state / "grants.sqlite")).execute("SELECT 1")
        except sqlite3.OperationalError:
            sql_blocked = True
        check("caller cannot open the broker database", sql_blocked)

        # 7. Rollback of the guard module cannot open the gate: with the
        #    broker stopped, every claim is denied while attempts race.
        broker.stop()
        denied_during = []

        def during():
            denied_during.append(not try_send(
                guard, account="acct", channel="C1", text="during rollback",
                tool_name="chat.postMessage"))

        racers = [threading.Thread(target=during) for _ in range(4)]
        for t in racers:
            t.start()
        for t in racers:
            t.join()
        check("rollback while attempts race stays denied", all(denied_during),
              f"denied={sum(denied_during)}/{len(denied_during)}")

        # 8. No send ever happened without a broker-accepted claim.
        check("no send without an accepted claim",
              SENDS["count"] == winners.count(True) + (1 if first else 0),
              f"sends={SENDS['count']}")

        # 9. Unknown tool and a closed marker both deny before any send.
        broker2 = BrokerProc(other_uid())
        broker2.start()
        guard2 = load_guard(broker2.sock)
        unknown = try_send(guard2, account="acct", channel="C1", text="x",
                           tool_name="conversations_add_message")
        check("unknown tool denies before send", not unknown)
        broker2.marker("stop")
        broker2.as_other("g.grant('acct', 'C1', 'closed marker')")
        closed = try_send(guard2, account="acct", channel="C1", text="closed marker",
                          tool_name="chat.postMessage")
        check("closed marker denies a real grant", not closed)
        broker2.stop()

    finally:
        broker.stop()

    failed = [name for name, ok in RESULTS if not ok]
    print(f"TOTAL {len(RESULTS)} FAILED {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
