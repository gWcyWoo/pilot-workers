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
- `== 完成` on success/exit and `!! ` on errors (Monitor greps these);
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

    HH:MM:SS |PID == 完成 exit=0 session=ses_xxx ==

Rules: every record is separated by a blank line; header line carries the
timestamp/PID/marker, content follows on the next line at a 4-space indent
(`name input → first informative output line`); read/edit/write show no
output (the path says it all); paths are shortened (`~`, last 3 segments);
newlines never leak into a content line.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

ROTATE_BYTES = 8_000_000
KEEP_ARCHIVES = 20
TEXT_LIMIT = 2_000
INPUT_LIMIT = 200
OUTPUT_LIMIT = 500
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


def _short_path(value: str) -> str:
    """~ for home, and keep only the last 3 segments of long paths."""
    home = str(Path.home())
    if value.startswith(home):
        value = "~" + value[len(home):]
    if len(value) > 64 and "/" in value:
        parts = value.split("/")
        if len(parts) > 4:
            value = "…/" + "/".join(parts[-3:])
    return value


def _first_line(value: str, limit: int) -> str:
    """First informative line, never a raw newline. Skips XML-ish wrapper lines."""
    for line in str(value).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("<") and line.endswith(">"):
            continue  # <path>…</path> / <type>file</type> / <content> wrappers
        if line.startswith("/") and " " not in line:
            line = _short_path(line)
        return _trim(line, limit)
    return ""


def _tool_input_brief(tool: str, tool_input: dict[str, Any]) -> str:
    for key in ("command", "filePath", "pattern", "path", "query", "url"):
        value = tool_input.get(key)
        if value:
            text = str(value)
            if key in ("filePath", "path"):
                text = _short_path(text)
            return _trim(text.replace("\n", " "), INPUT_LIMIT)
    return _trim(json.dumps(tool_input, ensure_ascii=False), INPUT_LIMIT) if tool_input else ""


# Tools whose output is a file dump or a plain confirmation — the input path
# already says everything; echoing the output only adds noise.
SILENT_OUTPUT_TOOLS = {"read", "edit", "write", "list", "todowrite"}


def render_event(event: dict[str, Any]) -> list[str]:
    """Render one event into log lines. Returns empty list = skip this event."""
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
        marker = "== 完成" if exit_code == 0 else "!! 失败"
        flags = "".join(
            f" [{name}]"
            for name in ("timed_out", "idle_timed_out", "interrupted")
            if event.get(name)
        )
        return [f"{marker} exit={exit_code}{flags} session={event.get('session_id')} =="]

    part = event.get("part") or {}

    if kind == "tool_use":
        state = part.get("state") or {}
        status = state.get("status")
        tool = part.get("tool", "?")
        brief = _tool_input_brief(tool, state.get("input") or {})

        if status == "error":
            reason = _first_line(state.get("error") or "", INPUT_LIMIT)
            return ["!! Tool:", f"{INDENT}{tool} {brief} → {reason}"]
        if status == "completed":
            if tool in SILENT_OUTPUT_TOOLS:
                return ["Tool:", f"{INDENT}{tool} {brief}"]
            output_brief = _first_line(state.get("output") or "", INPUT_LIMIT)
            if output_brief:
                return ["Tool:", f"{INDENT}{tool} {brief} → {output_brief}"]
            return ["Tool:", f"{INDENT}{tool} {brief}"]
        return []

    if kind == "reasoning":
        text = (part.get("text") or "").strip()
        if not text:
            return []
        content = _indent_multiline(text, TEXT_LIMIT)
        return [
            "Thinking:",
            f"{INDENT}{content}",
        ]

    if kind == "text":
        text = (part.get("text") or "").strip()
        if not text:
            return []
        return [f"💬 {_trim(text, TEXT_LIMIT)}"]

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
