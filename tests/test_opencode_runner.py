"""Tests for OpenCodeRunner.parse_events and related methods."""

import pytest
from pilot_workers.runners import get_runner
from pilot_workers.runners.opencode_runner import OpenCodeRunner


@pytest.fixture
def runner():
    return get_runner("opencode")


# --- parse_events branch coverage ---

def test_step_finish_extracts_tokens(runner):
    raw = {"type": "step_finish", "timestamp": 1000, "part": {
        "tokens": {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 3, "write": 1}}}}
    evs = runner.parse_events(raw)
    steps = [e for e in evs if e.kind == "step"]
    assert len(steps) == 1
    t = steps[0].tokens
    assert t.input == 10 and t.output == 5 and t.reasoning == 2
    assert t.cache_read == 3 and t.cache_write == 1
    assert steps[0].ts == 1000

def test_step_finish_without_tokens(runner):
    raw = {"type": "step_finish", "timestamp": 500, "part": {}}
    evs = runner.parse_events(raw)
    steps = [e for e in evs if e.kind == "step"]
    assert len(steps) == 1
    t = steps[0].tokens
    assert t.input == 0 and t.output == 0

def test_text_event(runner):
    raw = {"type": "text", "part": {"text": "hello world"}}
    evs = runner.parse_events(raw)
    texts = [e for e in evs if e.kind == "text"]
    assert len(texts) == 1 and texts[0].text == "hello world"

def test_reasoning_event(runner):
    raw = {"type": "reasoning", "part": {"text": "thinking..."}}
    evs = runner.parse_events(raw)
    rr = [e for e in evs if e.kind == "reasoning"]
    assert len(rr) == 1 and rr[0].text == "thinking..."

def test_tool_completed(runner):
    raw = {"type": "tool_use", "part": {"tool": "grep", "state": {
        "status": "completed", "input": {"pattern": "foo"}, "output": "Found 3\nx"}}}
    evs = runner.parse_events(raw)
    tools = [e for e in evs if e.kind == "tool"]
    assert len(tools) == 1
    tc = tools[0].tool
    assert tc.name == "grep" and tc.status == "completed"
    assert tc.input_brief == "foo"
    assert tc.output_brief == "Found 3"
    assert not tc.is_permission_denied and not tc.silent_output

def test_tool_permission_denied(runner):
    raw = {"type": "tool_use", "part": {"tool": "bash", "state": {
        "status": "error", "error": "blocked by rule which prevents this"}}}
    evs = runner.parse_events(raw)
    tools = [e for e in evs if e.kind == "tool"]
    assert tools[0].tool.is_permission_denied

def test_tool_silent_output(runner):
    for tool_name in ("read", "edit", "write", "list", "todowrite"):
        raw = {"type": "tool_use", "part": {"tool": tool_name, "state": {"status": "completed"}}}
        evs = runner.parse_events(raw)
        tools = [e for e in evs if e.kind == "tool"]
        assert tools[0].tool.silent_output, f"{tool_name} should be silent"

def test_error_event(runner):
    raw = {"type": "error", "timestamp": 999}
    evs = runner.parse_events(raw)
    errors = [e for e in evs if e.kind == "error"]
    assert len(errors) == 1 and errors[0].ts == 999

def test_session_extraction_sessionID(runner):
    raw = {"type": "step_finish", "sessionID": "ses_abc", "part": {"tokens": {}}}
    evs = runner.parse_events(raw)
    sessions = [e for e in evs if e.kind == "session"]
    assert len(sessions) == 1 and sessions[0].session_id == "ses_abc"

def test_session_extraction_sessionId(runner):
    raw = {"type": "step_finish", "sessionId": "ses_camel", "part": {"tokens": {}}}
    evs = runner.parse_events(raw)
    sessions = [e for e in evs if e.kind == "session"]
    assert len(sessions) == 1 and sessions[0].session_id == "ses_camel"

def test_session_extraction_session_id(runner):
    raw = {"type": "step_finish", "session_id": "ses_snake", "part": {"tokens": {}}}
    evs = runner.parse_events(raw)
    sessions = [e for e in evs if e.kind == "session"]
    assert len(sessions) == 1 and sessions[0].session_id == "ses_snake"

def test_malformed_part_skipped(runner):
    # non-dict part must not raise
    raw = {"type": "tool_use", "part": "not_a_dict"}
    evs = runner.parse_events(raw)
    # should not raise; may return empty or partial events
    assert isinstance(evs, list)

def test_unknown_event_returns_empty(runner):
    raw = {"type": "whatever", "data": 123}
    evs = runner.parse_events(raw)
    assert evs == []
