"""Tests for fmt_events.render_unified — verifies rendering equivalence."""

from pilot_workers.fmt_events import render_unified
from pilot_workers.runners.base import UnifiedEvent, ToolCall


def _rendered(ev):
    """render_unified returns a list of lines; join for substring checks."""
    return "\n".join(render_unified(ev))


def test_tool_completed_with_output():
    ev = UnifiedEvent(kind="tool", tool=ToolCall(
        name="grep", status="completed", input_brief="pattern:foo",
        output_brief="Found 3 matches", error=None,
        is_permission_denied=False, silent_output=False))
    line = _rendered(ev)
    assert "grep" in line and "pattern:foo" in line and "Found 3 matches" in line

def test_tool_error_permission_denied():
    ev = UnifiedEvent(kind="tool", tool=ToolCall(
        name="bash", status="error", input_brief="rm -rf /",
        output_brief="", error="blocked by rule which prevents this",
        is_permission_denied=True, silent_output=False))
    line = _rendered(ev)
    assert line.startswith("!! ")

def test_tool_silent_output():
    ev = UnifiedEvent(kind="tool", tool=ToolCall(
        name="read", status="completed", input_brief="/a/b.py",
        output_brief="line1 of file", error=None,
        is_permission_denied=False, silent_output=True))
    line = _rendered(ev)
    assert "read" in line
    # silent output tools should not display output_brief
    assert "line1 of file" not in line

def test_text_rendering():
    ev = UnifiedEvent(kind="text", text="Hello world")
    line = _rendered(ev)
    assert "Hello" in line

def test_reasoning_rendering():
    ev = UnifiedEvent(kind="reasoning", text="Let me think\nabout this")
    line = _rendered(ev)
    assert "Thinking" in line or "think" in line


def test_render_event_started():
    from pilot_workers.fmt_events import render_event
    lines = render_event({"type": "worker_runner.started", "provider": "glm",
        "model": "glm-worker/glm-5.2", "mode": "code", "run_id": "r1",
        "workdir": "/tmp", "log": "/tmp/r1.jsonl"})
    joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
    assert "glm" in joined


def test_render_event_summary_done():
    from pilot_workers.fmt_events import render_event
    lines = render_event({"type": "worker_runner.summary", "exit_code": 0,
        "session_id": "ses_x", "timed_out": False, "idle_timed_out": False,
        "interrupted": False})
    joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
    assert "DONE" in joined


def test_render_event_summary_failed():
    from pilot_workers.fmt_events import render_event
    lines = render_event({"type": "worker_runner.summary", "exit_code": 1,
        "timed_out": True, "idle_timed_out": False, "interrupted": False})
    joined = "\n".join(lines) if isinstance(lines, list) else str(lines)
    assert "FAILED" in joined or "timed_out" in joined
