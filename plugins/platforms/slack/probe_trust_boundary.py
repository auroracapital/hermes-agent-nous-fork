"""Negative same-UID trust-boundary probes for the Slack egress guard.

These are not unit tests of the guard's happy path (see test_egress_guard.py).
They answer one question: can code running as the SAME operating-system user
as the caller manufacture its own permission? The answer is yes, and this file
is the executed proof, kept so the finding is not just a claim in a message.

No network. No credentials. Every DB path is a throwaway tempdir.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "egress_guard.py"


def _load_fresh_module(db_path: str, marker_path: str):
    """Import egress_guard as a NEW module object with its own os.environ
    already set, so module-level nothing is shared between probes."""
    os.environ["SLACK_EGRESS_DB"] = db_path
    os.environ["SLACK_ESTOP_PATH"] = marker_path
    spec = importlib.util.spec_from_file_location("egress_guard_probe", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe_self_issued_grant() -> tuple[bool, str]:
    """Same process, same uid: import grant() directly and approve yourself.

    This is the exact API the guard exposes. Nothing exotic — a caller with
    read access to this file can do this today.
    """
    db = str(Path(tempfile.mkdtemp()) / "e.sqlite")
    marker = str(Path(tempfile.mkdtemp()) / "m")
    g = _load_fresh_module(db, marker)
    g.marker_path().write_text("open")
    g.grant("realaccount", "C-REAL", "wire funds now")
    try:
        g.claim("realaccount", "C-REAL", "wire funds now", approved=True, tool_name="chat.postMessage")
        return True, "self-issued grant() call produced a usable claim"
    except g.EgressDenied as exc:
        return False, str(exc)


def probe_db_path_override() -> tuple[bool, str]:
    """Same uid: point SLACK_EGRESS_DB at an attacker-writable file and seed
    a grant row with raw SQL, never calling grant() at all."""
    db = str(Path(tempfile.mkdtemp()) / "attacker.sqlite")
    marker = str(Path(tempfile.mkdtemp()) / "m")
    g = _load_fresh_module(db, marker)
    g.marker_path().write_text("open")
    fp = g.fingerprint("realaccount", "C-REAL", "wire funds now", "")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS grants (fp TEXT PRIMARY KEY, account TEXT, "
        "channel TEXT, body TEXT, attachment TEXT, consumed INTEGER NOT NULL DEFAULT 0)"
    )
    con.execute("INSERT INTO grants VALUES (?,?,?,?,?,0)",
                (fp, "realaccount", "C-REAL", "wire funds now", ""))
    con.commit()
    con.close()
    try:
        g.claim("realaccount", "C-REAL", "wire funds now", approved=True, tool_name="chat.postMessage")
        return True, "raw SQL row via env-overridden DB path produced a usable claim"
    except g.EgressDenied as exc:
        return False, str(exc)


def probe_marker_path_override() -> tuple[bool, str]:
    """Same uid: point SLACK_ESTOP_PATH at a file the attacker controls and
    writes 'open' to it themselves, bypassing any real operator-controlled
    stop marker."""
    db = str(Path(tempfile.mkdtemp()) / "e.sqlite")
    marker = str(Path(tempfile.mkdtemp()) / "attacker-marker")
    g = _load_fresh_module(db, marker)
    Path(marker).write_text("open")  # attacker writes their own "open"
    g.grant("realaccount", "C-REAL", "urgent wire")
    try:
        g.claim("realaccount", "C-REAL", "urgent wire", approved=True, tool_name="chat.postMessage")
        return True, "attacker-controlled marker path was accepted as the stop marker"
    except g.EgressDenied as exc:
        return False, str(exc)


def main() -> int:
    results = [
        ("self_issued_grant", probe_self_issued_grant()),
        ("db_path_override", probe_db_path_override()),
        ("marker_path_override", probe_marker_path_override()),
    ]
    exploitable = 0
    for name, (worked, detail) in results:
        tag = "EXPLOITABLE" if worked else "blocked"
        print(f"{tag} {name}: {detail}")
        if worked:
            exploitable += 1
    print(f"TOTAL {len(results)} EXPLOITABLE {exploitable}")
    print(
        "CONCLUSION: code-only same-process, same-uid separation cannot close "
        "this. Every probe above uses only the public module surface (or a "
        "file the same uid already owns). A caller and an approver sharing "
        "one process and one uid can always grant themselves permission. "
        "This requires a second owner outside caller-writable authority "
        "(separate OS user + local socket service, or an external signer), "
        "not another commit to this module."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
