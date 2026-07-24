#!/usr/bin/env python3
"""Render OpenCode JSON events into a human-readable live log.

Convenience layer only: the raw JSONL event log and the final
`worker_runner.summary` stay authoritative. Any failure in here must never
affect the worker run or its exit code — callers wrap every call and disable
rendering on the first error.

Conventions kept from the previous run.sh/fmt.py pipeline so existing habits
and monitors keep working:
- fixed per-provider live path `<logs>/<provider>/latest.log` for `tail -f`;
- every line tagged `|<PID>` so parallel workers stay distinguishable;
- `== DONE` on success/exit and `!! ` on errors (Monitor greps these);
- append-only writes, rotate by rename above 8 MB (BSD `tail -F` follows
  renames, not in-place truncation);
- per-run archives, pruned to the newest 20 per provider.

Log format (designed for human scanning in `tail -f`):

    HH:MM:SS |PID ════ glm code run=xxx ... ════

    HH:MM:SS |PID Thinking:
        The auth middleware checks JWT tokens...

    HH:MM:SS |PID 💬 Now let me create the logger file:

    HH:MM:SS |PID Tool:
        grep seat|products → Found 100 matches

    HH:MM:SS |PID !! Tool:
        bash curl http://x → denied by permission rule

    HH:MM:SS |PID == DONE exit=0 session=ses_xxx ==

Rules: every record is separated by a blank line; header line carries the
timestamp/PID/marker, content follows on the next line at a 4-space indent
(`name input → first informative output line`); read/edit/write show no
output (the path says it all); paths are shortened (`~`, last 3 segments);
newlines never leak into a content line.

Engine-specific event shape translation (tool_use/text/reasoning) is owned
by the runner adapter (see ``runners/opencode_runner.py``). This module now
renders two kinds of input:

- ``write_event(dict)`` for pilot-workers-owned events (worker_runner.*).
- ``write_unified(UnifiedEvent)`` for engine events already translated by a
  ``Runner.parse_events`` implementation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pilot_workers.runners.base import UnifiedEvent

ROTATE_BYTES = 8_000_000
KEEP_ARCHIVES = 20
TEXT_LIMIT = 2_000
INPUT_LIMIT = 200
INDENT = "    "


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _trim(value: Any, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _indent_multiline(text: str, limit: int) -> str:
    trimmed = _trim(text, limit)
    return trimmed.replace("\n", "\n" + INDENT)


def render_event(event: dict[str, Any]) -> list[str]:
    """Render one pilot-workers-owned event into log lines.

    Only ``worker_runner.*`` event types are handled here. Engine-native
    events (tool_use/text/reasoning) are translated into UnifiedEvent by the
    runner adapter and rendered via ``FmtWriter.write_unified``.
    """
    kind = event.get("type")

    if kind == "worker_runner.started":
        return [
            f"════ {event.get('provider')} {event.get('mode')} run={event.get('run_id')} "
            f"model={event.get('model')} @ {event.get('workdir')} ════"
        ]

    if kind == "worker_runner.heartbeat":
        return [
            f"… still running: elapsed {event.get('elapsed_s')}s, "
            f"silent {event.get('silent_s')}s"
        ]

    if kind == "worker_runner.summary":
        exit_code = event.get("exit_code")
        marker = "== DONE" if exit_code == 0 else "!! FAILED"
        flags = "".join(
            f" [{name}]"
            for name in ("timed_out", "idle_timed_out", "interrupted")
            if event.get(name)
        )
        return [f"{marker} exit={exit_code}{flags} session={event.get('session_id')} =="]

    return []


def render_unified(ev: UnifiedEvent) -> list[str]:
    """Render one UnifiedEvent into log lines.

    Behaviour matches the former ``render_event`` tool_use / reasoning / text
    branches exactly (those branches have been moved here from the dict
    rendering path, with input/output briefs supplied by the runner adapter
    rather than re-parsed locally).
    """
    if ev.kind == "tool":
        tool = ev.tool
        if tool is None:
            return []
        brief = tool.input_brief
        if tool.status == "error":
            reason = (tool.error or "").strip().splitlines()
            reason_text = reason[0].strip() if reason else ""
            reason_trimmed = _trim(reason_text, INPUT_LIMIT)
            return ["!! Tool:", f"{INDENT}{tool.name} {brief} → {reason_trimmed}"]
        if tool.status == "completed":
            if tool.silent_output:
                return ["Tool:", f"{INDENT}{tool.name} {brief}"]
            if tool.output_brief:
                return ["Tool:", f"{INDENT}{tool.name} {brief} → {tool.output_brief}"]
            return ["Tool:", f"{INDENT}{tool.name} {brief}"]
        return []

    if ev.kind == "reasoning":
        text = (ev.text or "").strip()
        if not text:
            return []
        content = _indent_multiline(text, TEXT_LIMIT)
        return [
            "Thinking:",
            f"{INDENT}{content}",
        ]

    if ev.kind == "text":
        text = (ev.text or "").strip()
        if not text:
            return []
        return [f"💬 {_trim(text, TEXT_LIMIT)}"]

    # step / error / session: no dedicated rendered line today.
    return []


class FmtWriter:
    """Append rendered lines to the fixed live log and slice a per-run archive."""

    def __init__(self, logs_dir: Path, provider_key: str, run_id: str, pid: int) -> None:
        self.logs_dir = logs_dir
        self.provider_key = provider_key
        self.run_id = run_id
        self.pid = pid
        self.latest = logs_dir / "latest.log"
        self.archive = logs_dir / f"rendered-{run_id}.log"
        self._rotate_if_needed()
        self.latest.touch(mode=0o600, exist_ok=True)
        self._offset = self.latest.stat().st_size

    def _rotate_if_needed(self) -> None:
        if self.latest.is_file() and self.latest.stat().st_size > ROTATE_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self.latest.rename(self.logs_dir / f"rendered-rotated-{stamp}.log")

    def write_lines(self, lines: list[str]) -> None:
        with self.latest.open("a", encoding="utf-8") as handle:
            prefix = f"{_clock()} |{self.pid}"
            handle.write(f"{prefix} {lines[0]}\n")
            for continuation in lines[1:]:
                handle.write(f"{continuation}\n")
            handle.write("\n")

    def write_event(self, event: dict[str, Any]) -> None:
        lines = render_event(event)
        if lines:
            self.write_lines(lines)

    def write_unified(self, ev: UnifiedEvent) -> None:
        lines = render_unified(ev)
        if lines:
            self.write_lines(lines)

    def finalize(self) -> None:
        with self.latest.open("rb") as handle:
            handle.seek(self._offset)
            payload = handle.read()
        descriptor = self.archive.open("wb")
        try:
            descriptor.write(payload)
        finally:
            descriptor.close()
        self.archive.chmod(0o600)
        self._prune_archives()

    def _prune_archives(self) -> None:
        archives = sorted(
            self.logs_dir.glob("rendered-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in archives[KEEP_ARCHIVES:]:
            stale.unlink(missing_ok=True)
