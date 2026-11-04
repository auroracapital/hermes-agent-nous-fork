"""Hermetic contracts for ordinary-session unfinished-todo stops."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import turn_stop_gates as gates
from tools.todo_tool import TodoStore


def agent_for(monkeypatch, *, status='pending', attempts=0):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_TODO_STOP', '1')
    monkeypatch.setattr(gates, '_verify_on_stop_nudge', lambda a: None)
    monkeypatch.setattr(gates, '_pre_verify_nudge', lambda *a: None)
    monkeypatch.setattr(gates, '_kanban_stop_nudge', lambda *a: None)
    monkeypatch.setattr(gates, '_late_lsp_verdicts', lambda a: None)
    monkeypatch.setattr('hermes_cli.goals.GoalManager.has_goal', lambda self: False)
    monkeypatch.setattr('tools.async_delegation.has_live_for_session', lambda **kw: False)
    store = TodoStore()
    store.write([{'id': 'one', 'content': 'Verify requested result', 'status': status}])
    return SimpleNamespace(session_id='fixture', platform='telegram', _todo_store=store,
        _todo_stop_nudges=attempts, _active_children=[], max_iterations=100,
        _emit_interim_assistant_message=Mock(), _flush_messages_to_session_db=Mock(),
        _interim_content_was_streamed=lambda text: False)


def apply(agent, response='Done.'):
    messages = [{'role': 'user', 'content': 'Do task'}]
    answer = {'role': 'assistant', 'content': response}
    result = gates.apply_stop_gates(agent, answer, final_response=response, messages=messages,
        conversation_history=[], pending_verification_response=None,
        pending_verification_response_previewed=False)
    return result, messages


@pytest.mark.parametrize('status', ['pending', 'in_progress'])
def test_open_todo_always_continues_even_with_done_claim(monkeypatch, status):
    agent = agent_for(monkeypatch, status=status)
    result, messages = apply(agent)
    assert result.continue_turn
    assert 'Verify requested result' in messages[-1]['content']
    assert agent._todo_store.read()[0]['status'] == status
    assert agent._todo_stop_nudges == 1
    assert [m['role'] for m in messages] == ['user', 'assistant', 'user']


@pytest.mark.parametrize('status', ['completed', 'cancelled'])
def test_terminal_todo_status_does_not_continue(monkeypatch, status):
    agent = agent_for(monkeypatch, status=status)
    assert not apply(agent)[0].continue_turn
    assert not getattr(agent, '_todo_stop_incomplete', False)


def test_empty_reply_still_continues(monkeypatch):
    agent = agent_for(monkeypatch)
    assert apply(agent, '')[0].continue_turn


def test_content_change_and_long_tail_cannot_hide_open_item(monkeypatch):
    agent = agent_for(monkeypatch)
    assert apply(agent)[0].continue_turn
    agent._todo_store.write([
        {'id': 'one', 'content': 'A' * 3500, 'status': 'pending'},
        {'id': 'last', 'content': 'Verify tail item', 'status': 'pending'},
    ])
    _, messages = apply(agent)
    assert 'last' in messages[-1]['content']
    assert 'Verify tail item' in messages[-1]['content']


@pytest.mark.parametrize('mode', ['interrupt', 'child', 'async', 'goal-paused', 'goal-active'])
def test_real_interrupt_or_owned_handoff_is_not_restarted(monkeypatch, mode):
    agent = agent_for(monkeypatch)
    if mode == 'interrupt': agent._interrupt_requested = True
    elif mode == 'child': agent._active_children = [SimpleNamespace(_interrupt_requested=False)]
    elif mode == 'async': monkeypatch.setattr('tools.async_delegation.has_live_for_session', lambda **kw: True)
    else: monkeypatch.setattr('hermes_cli.goals.GoalManager.has_goal', lambda self: True)
    assert not apply(agent)[0].continue_turn
    assert not getattr(agent, '_todo_stop_incomplete', False)


def test_cap_preserves_open_work_as_incomplete_not_completed(monkeypatch):
    agent = agent_for(monkeypatch, attempts=3)
    assert not apply(agent)[0].continue_turn
    assert agent._todo_stop_incomplete is True
    assert agent._todo_store.read()[0]['status'] == 'pending'


def test_iteration_cap_preserves_incomplete_work(monkeypatch):
    agent = agent_for(monkeypatch)
    agent.iteration_budget = SimpleNamespace(remaining=0)
    assert not apply(agent)[0].continue_turn
    assert agent._todo_stop_incomplete is True


def test_completion_clears_previous_incomplete_flag(monkeypatch):
    agent = agent_for(monkeypatch, attempts=3)
    apply(agent)
    agent._todo_store.write([{'id': 'one', 'content': 'Verify requested result', 'status': 'completed'}])
    assert not apply(agent)[0].continue_turn
    assert agent._todo_stop_incomplete is False


def test_ordinary_todo_gate_does_not_call_auxiliary_judge(monkeypatch):
    agent = agent_for(monkeypatch)
    judge = Mock(side_effect=AssertionError('No LLM completion cache'))
    monkeypatch.setattr('hermes_cli.goals.judge_goal', judge)
    assert apply(agent)[0].continue_turn
    judge.assert_not_called()


def test_human_wait_preserves_reason_without_consuming_nudge_budget(monkeypatch):
    from agent.own_todo_stop import bounded_incomplete_outcome
    agent = agent_for(monkeypatch, attempts=3)
    agent._decision_waiting = True
    assert not apply(agent, 'Waiting for approval.')[0].continue_turn
    reason = 'I reached the iteration limit and could not generate a summary.'
    result = bounded_incomplete_outcome(agent, 'Waiting for approval.', reason)
    assert result == ('Waiting for approval.', reason)
    assert agent._todo_stop_nudges == 3
    assert agent._todo_store.read()[0]['status'] == 'pending'


def test_bounded_gate_replaces_final_candidate_before_first_flush(monkeypatch):
    agent = agent_for(monkeypatch, attempts=3)
    result, messages = apply(agent, 'Everything is done.')
    assert result.final_response != 'Everything is done.'
    assert 'not finished' in result.final_response
    assert not result.continue_turn
    agent._flush_messages_to_session_db.assert_not_called()


def test_turn_reset_clears_counter_not_task(monkeypatch):
    from agent.turn_context import _reset_per_turn_agent_state
    agent = agent_for(monkeypatch, attempts=3)
    agent._todo_stop_incomplete = True
    agent._tool_guardrails = SimpleNamespace(reset_for_turn=Mock())
    agent._memory_store = SimpleNamespace(reset_consolidation_failures=Mock())
    agent.api_mode = 'anthropic_messages'
    agent._compression_warning = None
    _reset_per_turn_agent_state(agent)
    assert agent._todo_stop_nudges == 0
    assert agent._todo_stop_incomplete is False
    assert agent._todo_store.read()[0]['status'] == 'pending'
