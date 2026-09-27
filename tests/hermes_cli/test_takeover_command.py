"""The takeover command is a menu entry plus a safe inventory.

It never claims, reassigns, or closes work. Those are the dispatcher's jobs.
"""
import os
import sqlite3

import pytest

from hermes_cli.commands import COMMAND_REGISTRY
from hermes_cli.commands_platforms import telegram_menu_commands


def _command():
    return next(cmd for cmd in COMMAND_REGISTRY if cmd.name == "takeover")


def test_takeover_is_registered_for_the_telegram_menu(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", os.environ["HERMES_HOME"])
    command = _command()
    assert command.gateway_only is False
    assert command.cli_only is False
    assert len(command.description) <= 40
    assert "\u2014" not in command.description and "\u2013" not in command.description

    menu, _hidden = telegram_menu_commands(max_commands=100)
    assert ("takeover", command.description) in menu


def test_takeover_inventory_never_claims_or_changes_work(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))

    from hermes_cli import kanban_db
    from hermes_cli.kanban_db_connect import connect as kanban_connect
    from hermes_cli.takeover import takeover_inventory

    kanban_db.create_board("alpha", name="Alpha")
    with kanban_connect(board="alpha") as conn:
        ready = kanban_db.create_task(conn, title="Ready work", initial_status="running")
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL "
            "WHERE id = ?", (ready,)
        )
        running = kanban_db.create_task(conn, title="Running work", initial_status="running")
        blocked = kanban_db.create_task(conn, title="Blocked work", initial_status="blocked")
        before = {
            row["id"]: (row["status"], row["assignee"], row["claim_lock"])
            for row in conn.execute("SELECT id, status, assignee, claim_lock FROM tasks")
        }

    result = takeover_inventory()

    assert result["boards"][0]["slug"] == "alpha"
    counts = result["boards"][0]["counts"]
    assert counts == {"ready": 2, "running": 0, "blocked": 1, "review": 0, "todo": 0}
    assert result["boards"][0]["tasks"] == [
        {"id": blocked, "title": "Blocked work", "status": "blocked"},
        {"id": ready, "title": "Ready work", "status": "ready"},
        {"id": running, "title": "Running work", "status": "ready"},
    ]
    assert result["changed"] == 0

    with kanban_connect(board="alpha") as conn:
        after = {
            row["id"]: (row["status"], row["assignee"], row["claim_lock"])
            for row in conn.execute("SELECT id, status, assignee, claim_lock FROM tasks")
        }
    assert after == before
    assert sqlite3.connect(kanban_db.kanban_db_path("alpha")).execute(
        "SELECT COUNT(*) FROM tasks"
    ).fetchone()[0] == 3


def test_takeover_inventory_keeps_boards_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    from hermes_cli import kanban_db
    from hermes_cli.kanban_db_connect import connect as kanban_connect
    from hermes_cli.takeover import takeover_inventory

    for slug in ("shop", "hub"):
        kanban_db.create_board(slug)
        with kanban_connect(board=slug) as conn:
            kanban_db.create_task(conn, title=f"{slug} task", initial_status="running")
            conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL")

    result = takeover_inventory()
    found = {board["slug"]: board["counts"]["ready"] for board in result["boards"]}
    assert found["shop"] == 1
    assert found["hub"] == 1
    assert result["changed"] == 0
