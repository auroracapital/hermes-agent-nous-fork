"""The real text-response path must settle the safe row before its first flush."""
import agent.conversation_loop  # bootstrap before per-test production-home I/O protection
from types import SimpleNamespace
from unittest.mock import Mock

from agent.turn_final_response import finish_text_response
from tests.agent.test_own_todo_stop import agent_for


def test_bounded_outcome_matches_first_durable_flush(monkeypatch):
    agent = agent_for(monkeypatch, attempts=3)
    agent.api_mode = 'chat_completions'
    agent.valid_tool_names = []
    agent._strip_think_blocks = lambda text: text
    agent._has_content_after_think_block = bool
    agent._emit_pending_fallback_notice = Mock()
    agent._clear_status_buffer = Mock()
    agent._build_assistant_message = lambda message, reason: {
        'role': 'assistant', 'content': message.content, 'finish_reason': reason}
    agent.quiet_mode = True
    agent._stall_guards = False
    agent._looks_like_codex_intermediate_ack = lambda **kw: False
    agent._safe_print = Mock()
    snapshot = []
    agent._flush_messages_to_session_db = lambda messages, history: snapshot.extend(dict(m) for m in messages)
    monkeypatch.setattr('agent.turn_finalizer.apply_llm_output_transform',
                        lambda agent, response, **kw: (response, False, None))
    message = SimpleNamespace(content='Everything is done.', tool_calls=[], reasoning_details=[])
    messages = [{'role': 'user', 'content': 'Verify result'}]
    result = finish_text_response(agent, assistant_message=message, response=None,
        finish_reason='stop', messages=messages, api_messages=[], conversation_history=[],
        api_call_count=4, user_message='Verify result', active_system_prompt='',
        final_response=None, _turn_exit_reason='unknown', _preflight_compression_blocked=False,
        codex_ack_continuations=0, truncated_response_parts=[], length_continue_retries=0,
        _pending_verification_response=None, _pending_verification_response_previewed=False,
        effective_task_id='fixture')
    assert result.action == 'break'
    assert 'not finished' in result.final_response
    assert snapshot[-1]['content'] == result.final_response
    assert 'Everything is done.' not in snapshot[-1]['content']
    assert messages[-1]['content'] == result.final_response
