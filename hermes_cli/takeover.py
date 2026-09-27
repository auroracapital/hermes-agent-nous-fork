"""Read-only inventory of every local Kanban board.

The command reports open work. It never claims, reassigns, blocks, or closes
a task: the dispatcher owns those transitions.
"""
from __future__ import annotations

from hermes_cli import kanban_db
from hermes_cli.kanban_db_connect import connect as kanban_connect

OPEN_STATUSES = ("ready", "running", "blocked", "review", "todo")


def takeover_inventory() -> dict:
    """Return open work on every local board, leaving every row unchanged."""
    boards = []
    for meta in kanban_db.list_boards(include_archived=False):
        slug = meta["slug"]
        counts = {status: 0 for status in OPEN_STATUSES}
        tasks = []
        with kanban_connect(board=slug) as conn:
            rows = conn.execute(
                "SELECT id, title, status FROM tasks "
                "WHERE status IN ('ready', 'running', 'blocked', 'review', 'todo') "
                "ORDER BY status, created_at"
            ).fetchall()
        for row in rows:
            counts[row["status"]] += 1
            tasks.append({"id": row["id"], "title": row["title"], "status": row["status"]})
        if any(counts.values()):
            boards.append({"slug": slug, "counts": counts, "tasks": tasks})
    return {"boards": boards, "changed": 0}


def format_takeover(result: dict) -> str:
    """Render an inventory as plain lines. Nothing in the text is an action."""
    if not result["boards"]:
        return "No open work on any board. Nothing was changed."
    lines = ["Open work, nothing changed:"]
    for board in result["boards"]:
        counts = ", ".join(
            f"{count} {status}" for status, count in board["counts"].items() if count
        )
        lines.append(f"{board['slug']}: {counts}")
        for task in board["tasks"]:
            lines.append(f"- {task['status']}: {task['title']}")
    return "\n".join(lines)
