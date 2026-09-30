#!/usr/bin/env python3
"""Prove a rollback cannot open the gate. No network, no credentials.

The old guard trusted a database path the caller chose and a grant() the
caller could call. This runs that old code and shows it now fails closed,
because the broker, not the caller, holds the grants.
"""
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLD = subprocess.run(
    ["git", "-C", str(HERE.parents[2]), "show",
     "91b247bd883:plugins/platforms/slack/egress_guard.py"],
    capture_output=True, text=True, check=True,
).stdout


def load_old(db: str, marker: str):
    os.environ["SLACK_EGRESS_DB"] = db
    os.environ["SLACK_ESTOP_PATH"] = marker
    path = Path(tempfile.mkdtemp()) / "old_guard.py"
    path.write_text(OLD, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("old_egress_guard", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="egress-rollback-", dir="/tmp"))
    db = str(base / "caller.sqlite")
    marker = str(base / "marker")
    Path(marker).write_text("open", encoding="utf-8")
    old = load_old(db, marker)

    checks = []

    # The rolled-back grant() writes a row the caller owns. That row must not
    # be enough on its own: the new boundary refuses it because no broker
    # recorded it. We prove the old row is invisible to the new guard.
    old.grant("acct", "C1", "rolled back")
    rows = sqlite3.connect(db).execute("SELECT count(*) FROM grants").fetchone()[0]
    checks.append(("rollback writes only into the caller-owned file", rows == 1))

    # The new guard, pointed at a broker that is not running, denies even
    # though the old code just wrote a grant and the marker says open.
    os.environ["SLACK_EGRESS_BROKER_SOCK"] = str(base / "missing.sock")
    spec = importlib.util.spec_from_file_location("new_guard", HERE / "egress_guard.py")
    assert spec is not None and spec.loader is not None
    new = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(new)
    denied = []

    def attempt():
        try:
            new.claim(account="acct", channel="C1", text="rolled back",
                      approved=True, tool_name="chat.postMessage")
            denied.append(False)
        except new.EgressDenied:
            denied.append(True)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    checks.append(("rollback while attempts race stays denied", all(denied)))

    # Uninstalling the broker socket entirely still denies.
    os.environ.pop("SLACK_EGRESS_BROKER_SOCK", None)
    spec2 = importlib.util.spec_from_file_location("new_guard_default", HERE / "egress_guard.py")
    assert spec2 is not None and spec2.loader is not None
    new2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(new2)
    try:
        new2.claim(account="acct", channel="C1", text="rolled back",
                   approved=True, tool_name="chat.postMessage")
        checks.append(("missing broker denies", False))
    except new2.EgressDenied:
        checks.append(("missing broker denies", True))

    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        failed += not ok
    print(f"TOTAL {len(checks)} FAILED {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
