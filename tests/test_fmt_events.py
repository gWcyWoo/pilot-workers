"""Rendering tests for pilot_workers.fmt_events.

The rendered log is a convenience layer — a failure in it must never affect a
worker run — which is exactly why it had no tests: nothing downstream breaks
when it is wrong, so a format regression would only ever be noticed by a human
reading logs, and only if they knew what they were looking for.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# "newlines never leak into a content line" — the module's own stated rule
# ---------------------------------------------------------------------------


def _record_lines(ev):
    from pilot_workers import fmt_events
    return fmt_events.render_unified(ev)


def test_multiline_text_is_indented_not_leaked():
    """The text branch interpolated the payload straight into the header line,
    so a worker's multi-line final text put later lines at column 0 — where a
    reader cannot tell them from a new record."""
    from pilot_workers.runners.base import UnifiedEvent

    lines = _record_lines(UnifiedEvent(kind="text", text="first\nsecond\nthird"))
    assert lines[0] == "💬", "the header line must carry no payload"
    body = "\n".join(lines[1:])
    for piece in ("first", "second", "third"):
        assert piece in body
    for line in body.split("\n"):
        assert line.startswith("    "), f"unindented continuation: {line!r}"


def test_multiline_reasoning_is_indented_too():
    from pilot_workers.runners.base import UnifiedEvent

    lines = _record_lines(UnifiedEvent(kind="reasoning", text="a\nb"))
    assert lines[0] == "Thinking:"
    for line in "\n".join(lines[1:]).split("\n"):
        assert line.startswith("    ")


@pytest.mark.parametrize("kind", ["step", "error", "session"])
def test_kinds_with_no_rendering_produce_nothing(kind):
    from pilot_workers.runners.base import UnifiedEvent

    assert _record_lines(UnifiedEvent(kind=kind, text="x")) == []


def test_empty_text_renders_nothing():
    from pilot_workers.runners.base import UnifiedEvent

    assert _record_lines(UnifiedEvent(kind="text", text="   ")) == []
    assert _record_lines(UnifiedEvent(kind="text", text=None)) == []


def test_every_written_record_has_exactly_one_prefixed_header(tmp_path):
    """The `|PID` prefix is how a reader separates records of parallel workers,
    so exactly one line per record may carry it."""
    from pilot_workers import fmt_events
    from pilot_workers.runners.base import UnifiedEvent

    writer = fmt_events.FmtWriter(tmp_path, "glm", "run-1", 4242)
    writer.write_unified(UnifiedEvent(kind="text", text="one\ntwo"))
    written = (tmp_path / "latest.log").read_text(encoding="utf-8")
    assert written.count("|4242") == 1, written
