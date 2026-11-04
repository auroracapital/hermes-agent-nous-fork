"""Deterministic turn-end guard for a session's own open todo list.

Do not infer execution from an LLM saying DONE, cache that verdict, or silently
mark tasks complete. Explicit goal/worker lifecycles and real async handoffs
remain owned by their existing controllers. Bounded exhaustion is INCOMPLETE,
not task completion. This is a lifecycle guard, not an execution-evidence judge.
"""
from __future__ import annotations

import logging
import os
from contextlib import nullcontext

logger = logging.getLogger(__name__)
MAX_NUDGES = 3
INCOMPLETE_NOTICE = (
    "Work is not finished: this session still has open todo items. "
    "The bounded continuation limit was reached; the items remain open. "
    "No task completion has been recorded."
)


def open_todos(agent):
    store = getattr(agent, '_todo_store', None)
    if store is None:
        return []
    return [row for row in store.read()
            if isinstance(row, dict) and row.get('status') in ('pending', 'in_progress')]


def build_own_todo_nudge(agent):
    """Return a continuation, or flag incomplete work at the safe bounded exit."""
    agent._todo_stop_incomplete = False
    # Structured state from the existing decision controller, not a prose guess.
    if getattr(agent, '_decision_waiting', False) is True:
        return None
    if getattr(agent, '_interrupt_requested', False):
        return None
    if os.environ.get('HERMES_TODO_STOP', '').lower() in ('0', 'false', 'off'):
        return None
    from agent.delegation_context import owned_kanban_task
    if owned_kanban_task():
        return None
    # These controllers must not be bypassed, nor may a paused goal be resumed.
    if str(getattr(agent, 'platform', '')).lower() == 'cron':
        return None
    from hermes_cli.goals import GoalManager
    sid = str(getattr(agent, 'session_id', '') or '')
    if sid and GoalManager(sid).has_goal():
        return None
    lock = getattr(agent, '_active_children_lock', None)
    with lock if lock is not None else nullcontext():
        children = list(getattr(agent, '_active_children', []) or [])
    if any(not getattr(child, '_interrupt_requested', False) for child in children):
        return None
    from tools.async_delegation import has_live_for_session
    if sid and has_live_for_session(parent_session_id=sid):
        return None
    rows = open_todos(agent)
    if not rows:
        return None
    budget = getattr(agent, 'iteration_budget', None)
    if (getattr(agent, '_todo_stop_nudges', 0) >= MAX_NUDGES
            or (budget is not None and budget.remaining <= 0)):
        agent._todo_stop_incomplete = True
        return None
    # Every item's ID/status is represented, even when one description is long.
    listing = '\n'.join(
        f"- {row.get('id', '?')} [{row['status']}]: {str(row.get('content', ''))[:180]}"
        for row in rows
    )
    return (
        '[System: This session has unfinished todo items:\n' + listing + '\n\n'
        'Continue the assigned work now, rather than announcing future work. '
        'Use todo_list to read complete descriptions. Mark an item completed only '
        'after verifying the actual result; never complete, cancel or delete items '
        'just to end the turn. Preserve human approval and explicit stop/pause. '
        'If a real human gate or unavailable capability prevents safe progress, '
        'record the exact blocker and keep the work open; do not bypass it.]'
    )


def bounded_incomplete_outcome(agent, final_response, exit_reason):
    """Avoid emitting a DONE claim when continuation ended with open work."""
    # The real iteration-limit path already names the wait. Keep that text
    # and reason; do not replace them with an unfinished-todo notice.
    reason = str(exit_reason or '')
    if 'iteration limit' in reason.lower():
        return final_response, reason
    if getattr(agent, '_todo_stop_incomplete', False) and open_todos(agent):
        return INCOMPLETE_NOTICE, 'unfinished_todos'
    return final_response, exit_reason
