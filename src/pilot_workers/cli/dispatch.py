#!/usr/bin/env python3
"""Deterministic outer shell around cli/run.py.

Wraps cli/run.py: launches it as a subprocess, forwards exactly one line
(the `worker_runner.started` event) to its own stdout, swallows the rest of
the child's stdout (it is preserved in the JSONL event log on disk), waits
for the child to exit, parses the JSONL, and prints a final
`worker_runner.verdict` line.

Stdout contract (callers depend on it): exactly two JSON lines, in order --
the forwarded `started` event and the final `verdict`. Nothing else is ever
written to stdout by this script.

Also supports a reparse mode (`--reparse <jsonl> --mode <mode>`) that skips
the dispatch step and recomputes a verdict for an existing JSONL event log;
this is used to post-mortem / re-harvest historical runs.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from pilot_workers import policy
from pilot_workers.runners import RUNNERS, Runner, get_runner


DEFAULT_TIMEOUT_S = 3600
DEFAULT_IDLE_TIMEOUT_S = 900
DISPATCH_ERROR_EXIT = 2
VERDICT_SCHEMA_VERSION = 1
EMPTY_FINAL_TEXT_THRESHOLD = 200

DISPATCH_ARG_NAMES = (
    "provider",
    "workdir",
    "task",
    "task_file",
    "session",
    "worktree",
    "timeout",
    "idle_timeout",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic outer shell around cli/run.py: dispatches a worker "
            "and prints started + verdict JSON, or reparses an existing run."
        )
    )
    parser.add_argument(
        "--reparse",
        metavar="JSONL",
        default=None,
        help=(
            "Skip dispatch and recompute a verdict for an existing JSONL event "
            "log. Must be combined with --mode and no dispatch arguments."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=sorted(policy.MODE_TO_AGENT),
        default=None,
        help="Worker mode; selects the step cap and agent.",
    )
    parser.add_argument(
        "--provider",
        default=argparse.SUPPRESS,
        help="Provider key (e.g. glm or kimi-k3).",
    )
    parser.add_argument(
        "--workdir",
        default=argparse.SUPPRESS,
        help="Existing project directory passed to the worker.",
    )
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument(
        "--task",
        default=argparse.SUPPRESS,
        help="Short task contract passed inline.",
    )
    task_group.add_argument(
        "--task-file",
        default=argparse.SUPPRESS,
        help="UTF-8 file containing the task contract.",
    )
    parser.add_argument(
        "--session",
        default=argparse.SUPPRESS,
        help="OpenCode session ID (resume mode only).",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Create a detached clean worktree from committed HEAD.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=argparse.SUPPRESS,
        help=f"Wall-clock limit in seconds (default {DEFAULT_TIMEOUT_S}; 0 disables).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=argparse.SUPPRESS,
        help=(
            "Kill the worker after this many seconds without output "
            f"(default {DEFAULT_IDLE_TIMEOUT_S}; 0 disables)."
        ),
    )
    parser.add_argument(
        "--runner",
        choices=sorted(RUNNERS),
        default="opencode",
        help="Runner adapter name (reparse mode only). Default: opencode.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# JSONL event-log parsing
# ---------------------------------------------------------------------------


def parse_jsonl(path: Path, runner: Runner) -> dict[str, Any]:
    """Extract verdict inputs from a runner JSONL event log.

    ``runner.parse_events`` translates each raw line into 0..n UnifiedEvents;
    the aggregation below is runner-agnostic. Lines that fail json.loads or
    parse_events translation are silently skipped.
    """
    steps = 0
    tokens = {
        "input": 0,
        "output": 0,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    tool_errors = {"permission_denied": 0, "other": 0}
    final_text = ""
    has_error_event = False
    first_ts: int | None = None
    last_ts: int | None = None
    session_id: str | None = None

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            try:
                unified = runner.parse_events(event)
            except Exception:
                continue
            for ev in unified:
                if ev.ts is not None:
                    if first_ts is None:
                        first_ts = ev.ts
                    last_ts = ev.ts
                if ev.kind == "step":
                    steps += 1
                    if ev.tokens is not None:
                        tokens["input"] += ev.tokens.input
                        tokens["output"] += ev.tokens.output
                        tokens["reasoning"] += ev.tokens.reasoning
                        tokens["cache_read"] += ev.tokens.cache_read
                        tokens["cache_write"] += ev.tokens.cache_write
                elif ev.kind == "text":
                    if ev.text:
                        final_text = ev.text
                elif ev.kind == "tool":
                    if ev.tool is not None and ev.tool.status == "error":
                        if ev.tool.is_permission_denied:
                            tool_errors["permission_denied"] += 1
                        else:
                            tool_errors["other"] += 1
                elif ev.kind == "session":
                    if ev.session_id:
                        session_id = ev.session_id
                elif ev.kind == "error":
                    has_error_event = True

    if first_ts is not None and last_ts is not None:
        duration_s: int | None = (last_ts - first_ts) // 1000
    else:
        duration_s = None

    return {
        "steps": steps,
        "tokens": tokens,
        "tool_errors": tool_errors,
        "final_text": final_text,
        "has_error_event": has_error_event,
        "duration_s": duration_s,
        "session_id": session_id,
    }


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def classify_verdict(
    parsed: dict[str, Any],
    step_cap: int,
    summary: dict[str, Any] | None,
) -> str:
    """Apply the fixed-order verdict rules; first match wins."""
    final_text = parsed["final_text"]
    if summary is not None:
        exit_code = summary.get("exit_code")
        if (
            (isinstance(exit_code, int) and exit_code != 0)
            or bool(summary.get("timed_out"))
            or bool(summary.get("idle_timed_out"))
            or bool(summary.get("interrupted"))
        ):
            return "error"
    else:
        if parsed["has_error_event"] and len(final_text) < EMPTY_FINAL_TEXT_THRESHOLD:
            return "error"
    if parsed["steps"] >= step_cap:
        return "step_capped_partial"
    if len(final_text) < EMPTY_FINAL_TEXT_THRESHOLD:
        return "empty"
    return "completed"


def build_verdict(
    *,
    run_id: str,
    provider: str | None,
    runner: str | None,
    mode: str,
    parsed: dict[str, Any],
    summary: dict[str, Any] | None,
    jsonl_path: str,
    stderr_path: str | None,
    step_cap: int,
) -> dict[str, Any]:
    verdict = classify_verdict(parsed, step_cap, summary)
    final_text = parsed["final_text"]
    if summary is not None:
        exit_code: Any = summary.get("exit_code")
        timed_out = bool(summary.get("timed_out"))
        idle_timed_out = bool(summary.get("idle_timed_out"))
        interrupted = bool(summary.get("interrupted"))
        session_id: Any = summary.get("session_id")
    else:
        exit_code = None
        timed_out = False
        idle_timed_out = False
        interrupted = False
        session_id = parsed.get("session_id")

    return {
        "type": "worker_runner.verdict",
        "schema_version": VERDICT_SCHEMA_VERSION,
        "run_id": run_id,
        "provider": provider,
        "runner": runner,
        "mode": mode,
        "verdict": verdict,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "idle_timed_out": idle_timed_out,
        "interrupted": interrupted,
        "steps": parsed["steps"],
        "step_cap": step_cap,
        "duration_s": parsed["duration_s"],
        "tokens": parsed["tokens"],
        "tool_errors": parsed["tool_errors"],
        "final_text_len": len(final_text),
        "final_text": final_text,
        "jsonl_path": jsonl_path,
        "stderr_path": stderr_path,
        "session_id": session_id,
    }


def write_verdict_file(path: Path, verdict: dict[str, Any]) -> None:
    """Write the verdict JSON to disk with 0600 permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(verdict, ensure_ascii=False))
            handle.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reparse mode
# ---------------------------------------------------------------------------


def run_reparse(jsonl_arg: str, mode: str, runner_name: str = "opencode") -> int:
    jsonl_path = Path(jsonl_arg).expanduser().resolve()
    if not jsonl_path.is_file():
        print(f"error: jsonl not found: {jsonl_path}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    if mode not in policy.STEPS_BY_MODE:
        print(f"error: unknown mode: {mode}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    runner = get_runner(runner_name)
    try:
        parsed = parse_jsonl(jsonl_path, runner)
    except OSError as exc:
        print(f"error: cannot read jsonl: {exc}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    run_id = jsonl_path.stem
    verdict = build_verdict(
        run_id=run_id,
        provider=None,
        runner=runner_name,
        mode=mode,
        parsed=parsed,
        summary=None,
        jsonl_path=str(jsonl_path),
        stderr_path=None,
        step_cap=policy.STEPS_BY_MODE[mode],
    )
    sys.stdout.write(json.dumps(verdict, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Dispatch mode
# ---------------------------------------------------------------------------


def _validate_dispatch_args(
    mode: str | None,
    provider: str | None,
    workdir: str | None,
    task: str | None,
    task_file: str | None,
) -> None:
    if mode is None:
        raise RuntimeError("--mode is required")
    if provider is None:
        raise RuntimeError("--provider is required")
    if workdir is None:
        raise RuntimeError("--workdir is required")
    if task is None and task_file is None:
        raise RuntimeError("one of --task or --task-file is required")


def _build_runner_command(
    provider: str,
    mode: str,
    workdir: str,
    task: str | None,
    task_file: str | None,
    session: str | None,
    worktree: bool,
    timeout: int,
    idle_timeout: int,
) -> list[str]:
    cmd: list[str] = [
        sys.executable,
        "-m", "pilot_workers.cli.run",
        "--provider",
        provider,
        "--mode",
        mode,
        "--workdir",
        workdir,
    ]
    if task is not None:
        cmd.extend(["--task", task])
    else:
        assert task_file is not None
        cmd.extend(["--task-file", task_file])
    if session:
        cmd.extend(["--session", session])
    if worktree:
        cmd.append("--worktree")
    cmd.extend(["--timeout", str(timeout)])
    cmd.extend(["--idle-timeout", str(idle_timeout)])
    return cmd


def run_dispatch(args: argparse.Namespace) -> int:
    mode = args.mode
    provider = getattr(args, "provider", None)
    workdir = getattr(args, "workdir", None)
    task = getattr(args, "task", None)
    task_file = getattr(args, "task_file", None)
    session = getattr(args, "session", None)
    worktree = bool(getattr(args, "worktree", False))
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT_S)
    idle_timeout = getattr(args, "idle_timeout", DEFAULT_IDLE_TIMEOUT_S)

    if getattr(args, "runner", "opencode") != "opencode":
        print(
            "error: --runner is only valid with --reparse; dispatch mode "
            "determines the runner from the provider",
            file=sys.stderr,
        )
        return DISPATCH_ERROR_EXIT

    _validate_dispatch_args(mode, provider, workdir, task, task_file)

    cmd = _build_runner_command(
        provider, mode, workdir, task, task_file,
        session, worktree, timeout, idle_timeout,
    )

    started: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    child_stderr_text = ""
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as child_stderr:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=child_stderr,
                text=True,
                bufsize=1,
                cwd=os.environ.get("TMPDIR") or "/tmp",
            )
        except OSError as exc:
            print(f"error: cannot start runner: {exc}", file=sys.stderr)
            return DISPATCH_ERROR_EXIT

        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "worker_runner.started" and started is None:
                    started = event
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                elif etype == "worker_runner.summary":
                    summary = event
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            print("error: interrupted", file=sys.stderr)
            return DISPATCH_ERROR_EXIT

        child_stderr.flush()
        child_stderr.seek(0)
        child_stderr_text = child_stderr.read()

    if started is None:
        print(
            "error: runner never emitted worker_runner.started",
            file=sys.stderr,
        )
        if child_stderr_text.strip():
            print(child_stderr_text.rstrip(), file=sys.stderr)
        return DISPATCH_ERROR_EXIT

    log_path_str = started.get("log")
    stderr_log_str = started.get("stderr_log")
    run_id = started.get("run_id")
    provider_from_started = started.get("provider")
    runner_name = started.get("runner") or "opencode"
    if not isinstance(log_path_str, str) or not isinstance(run_id, str):
        print("error: started event missing log/run_id", file=sys.stderr)
        return DISPATCH_ERROR_EXIT

    log_path = Path(log_path_str)
    if not log_path.is_file():
        print(f"error: jsonl not found: {log_path}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    runner = get_runner(runner_name)
    try:
        parsed = parse_jsonl(log_path, runner)
    except OSError as exc:
        print(f"error: cannot read jsonl: {exc}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT

    step_cap = policy.STEPS_BY_MODE.get(mode, 0)
    verdict = build_verdict(
        run_id=run_id,
        provider=provider_from_started,
        runner=runner_name,
        mode=mode,
        parsed=parsed,
        summary=summary,
        jsonl_path=str(log_path),
        stderr_path=stderr_log_str,
        step_cap=step_cap,
    )

    verdict_path = log_path.parent / f"{run_id}.verdict.json"
    try:
        write_verdict_file(verdict_path, verdict)
    except OSError as exc:
        print(f"error: cannot write verdict file: {exc}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT

    sys.stdout.write(json.dumps(verdict, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    namespace_dict = vars(args)
    has_reparse = namespace_dict.get("reparse") is not None
    present_dispatch_args = [n for n in DISPATCH_ARG_NAMES if n in namespace_dict]

    if has_reparse:
        if present_dispatch_args:
            listed = ", ".join(
                "--" + n.replace("_", "-") for n in present_dispatch_args
            )
            print(
                f"error: --reparse cannot be combined with dispatch arguments: {listed}",
                file=sys.stderr,
            )
            return DISPATCH_ERROR_EXIT
        if args.mode is None:
            print("error: --mode is required with --reparse", file=sys.stderr)
            return DISPATCH_ERROR_EXIT
        return run_reparse(args.reparse, args.mode, args.runner)

    try:
        return run_dispatch(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
