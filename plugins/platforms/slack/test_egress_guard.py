"""Isolated tests for the Slack egress guard. No network, no credentials."""
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["SLACK_EGRESS_DB"] = str(Path(tempfile.mkdtemp()) / "egress.sqlite")
os.environ["SLACK_ESTOP_PATH"] = str(Path(tempfile.mkdtemp()) / "slack-estop")

from plugins.platforms.slack.egress_guard import (  # noqa: E402
    EgressDenied, claim, classify, grant, mark, marker_path,
)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (" " + detail if detail else ""))


def marker(word):
    marker_path().write_text(word, encoding="utf-8")


def denied(fn):
    try:
        fn()
    except EgressDenied:
        return True
    return False


def main():
    marker_path().unlink(missing_ok=True)
    check("marker missing denies", denied(lambda: claim("a", "C1", "hi", approved=True)))
    marker("")
    check("marker empty denies", denied(lambda: claim("a", "C1", "hi", approved=True)))
    marker("stop")
    check("marker stop denies", denied(lambda: claim("a", "C1", "hi", approved=True)))
    marker("open")
    # A caller that says approved=True without a recorded grant is denied.
    check("caller true without grant denies", denied(
        lambda: claim("a", "C1", "hi", approved=True)))
    # A grant for different text does not cover this text.
    grant("a", "C1", "other text")
    check("scope mismatch denies", denied(
        lambda: claim("a", "C1", "hi", approved=True)))
    check("unknown tool denies", denied(
        lambda: claim("a", "C1", "hi", approved=True, tool_name="conversations_add_message")))
    check("read allowlisted", classify("conversations_history") == "read")
    check("read skips claim", claim("a", "C1", "hi", tool_name="channels_list") == "read")

    grant("acct", "C1", "hello", attachment="f.png")
    fp = claim("acct", "C1", "hello", attachment="f.png", approved=True,
               tool_name="chat.postMessage")
    check("approved claim inserts", bool(fp) and fp != "read")
    check("same text denied", denied(
        lambda: claim("acct", "C1", "hello", attachment="f.png", approved=True,
                      tool_name="chat.postMessage")))

    # Two processes, one grant: exactly one insert, the other loses.
    grant("acct", "C2", "race")
    barrier = threading.Barrier(2)
    wins = []

    def race():
        barrier.wait()
        try:
            wins.append(claim("acct", "C2", "race", approved=True, tool_name="chat.postMessage"))
        except EgressDenied:
            wins.append(None)

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("race one winner", wins.count(None) == 1 and sum(bool(w) for w in wins) == 1,
          str(wins))

    # Crash after the effect: the grant is consumed, so a replay stays blocked.
    grant("acct", "C3", "crash")
    fp2 = claim("acct", "C3", "crash", approved=True, tool_name="files_upload_v2")
    mark(fp2, "uncertain")
    check("uncertain stays blocked", denied(
        lambda: claim("acct", "C3", "crash", approved=True, tool_name="files_upload_v2")))

    # Different attachment is a different effect and needs its own grant.
    grant("acct", "C3", "crash", attachment="other.png")
    fp3 = claim("acct", "C3", "crash", attachment="other.png", approved=True,
                tool_name="files_upload_v2")
    check("different attachment claims", bool(fp3))

    # Two processes racing on the SAME grant: only one consumes it.
    grant("acct", "C4", "grant race")
    barrier2 = threading.Barrier(2)
    wins2 = []

    def race2():
        barrier2.wait()
        try:
            wins2.append(claim("acct", "C4", "grant race", approved=True,
                                tool_name="chat.postMessage"))
        except EgressDenied:
            wins2.append(None)

    threads2 = [threading.Thread(target=race2) for _ in range(2)]
    for t in threads2:
        t.start()
    for t in threads2:
        t.join()
    check("grant race one winner", wins2.count(None) == 1 and sum(bool(w) for w in wins2) == 1,
          str(wins2))

    # A caller passing approved=True twice against the same consumed grant
    # cannot spoof a second send.
    check("consumed grant blocks replay", denied(
        lambda: claim("acct", "C4", "grant race", approved=True, tool_name="chat.postMessage")))

    con = sqlite3.connect(os.environ["SLACK_EGRESS_DB"])
    rows = con.execute("SELECT status, count(*) FROM claims GROUP BY status").fetchall()
    con.close()
    print("ROWS", rows)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("TOTAL", len(RESULTS), "FAILED", len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
