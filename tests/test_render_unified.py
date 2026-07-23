"""Tests for fmt_events.render_unified — verifies rendering equivalence."""

from pilot_workers.fmt_events import render_unified
from pilot_workers.runners.base import UnifiedEvent, ToolCall, TokenUsage


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
