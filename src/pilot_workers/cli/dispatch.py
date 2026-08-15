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
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any

from pilot_workers import policy, runtime
from pilot_workers.runners import RUNNERS, Runner, get_runner


DEFAULT_TIMEOUT_S = 3600
DEFAULT_IDLE_TIMEOUT_S = 900
DISPATCH_ERROR_EXIT = 2
VERDICT_SCHEMA_VERSION = 2
# The runner assumed when a historical log does not name one.
DEFAULT_RUNNER = "opencode"
EMPTY_FINAL_TEXT_THRESHOLD = 200
# How much of the child's stderr an error/empty verdict carries inline. Same
# budget fanout uses for its synthesized verdicts, so the two agree.
STDERR_TAIL_BYTES = 500
RESULT_BEGIN = "<!--PILOT_RESULT_BEGIN-->"
RESULT_END = "<!--PILOT_RESULT_END-->"
NO_MODEL_OUTPUT_SENTINEL = "no model output"
HEARTBEAT_LINE_PREFIX = "pilot-workers-heartbeat"
EPILOGUE_HEARTBEAT_SECONDS = 5

DISPATCH_ARG_NAMES = (
    "provider",
    "workdir",
    "task",
    "task_file",
    "session",
    "run_id",
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
        # Cannot be argparse-required: --reparse legitimately omits it. Say so
        # here, or the help invites a command that then exits 2.
        help="Provider key, e.g. glm (required for a dispatch; "
             "omitted only with --reparse).",
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
        "--run-id",
        default=argparse.SUPPRESS,
        help="Run ID of the original run (resume mode only).",
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
        # No default: dispatch mode must be able to tell "not passed" from
        # "passed the default", and reparse fills in DEFAULT_RUNNER itself.
        # Comparing against the literal "opencode" made the guard silently
        # wrong the day the default changed.
        default=None,
        help=f"Runner adapter name (reparse mode only). Default: {DEFAULT_RUNNER}.",
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
# Structured result extraction (D3)
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_discuss_result(payload: Any) -> bool:
    """A position, why, and what would overturn it.

    `choice` is nullable on purpose: an open-ended question ("how should
    this be structured") names no options to pick between, and forcing one
    would make the worker invent a false dichotomy. `would_change_if` is
    NOT optional — it is the field that makes disagreement actionable, by
    telling the planner which evidence is worth going to find.
    """
    if not isinstance(payload, dict):
        return False
    position = payload.get("position")
    if not isinstance(position, str) or not position.strip():
        return False
    choice = payload.get("choice")
    if choice is not None and not isinstance(choice, str):
        return False
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, list) or not reasoning:
        return False
    for item in reasoning:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("point"), str):
            return False
        if not isinstance(item.get("evidence"), str):
            return False
    if not isinstance(payload.get("risks"), str):
        return False
    changer = payload.get("would_change_if")
    return isinstance(changer, str) and bool(changer.strip())


def _validate_explore_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return False
    for fact in facts:
        if not isinstance(fact, dict):
            return False
        if not isinstance(fact.get("fact"), str):
            return False
        if not isinstance(fact.get("file_line"), str):
            return False
    truncated = payload.get("truncated")
    if not isinstance(truncated, bool):
        return False
    # prompts/explore.md asks for more_in "if truncated". Requiring it always
    # scored a complete exploration as unstructured; requiring it when truncated
    # is what the prompt actually promises the worker.
    more_in = payload.get("more_in")
    if truncated:
        return _is_str_list(more_in)
    return more_in is None or _is_str_list(more_in)


CODE_STATUSES = ("complete", "partial", "blocked")


def _validate_code_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    # The enum is enforced, like review's severity: prompts/code.md documents
    # `<complete|partial|blocked>`, and a status the planner cannot interpret is
    # no more useful than a missing one.
    if payload.get("status") not in CODE_STATUSES:
        return False
    if not _is_str_list(payload.get("files_changed")):
        return False
    validation = payload.get("validation")
    if not isinstance(validation, dict):
        return False
    commands = validation.get("commands")
    if not isinstance(commands, list):
        return False
    for command in commands:
        if not isinstance(command, dict):
            return False
        if not isinstance(command.get("cmd"), str):
            return False
        if not _is_int(command.get("exit_code")):
            return False
        if not isinstance(command.get("output_summary"), str):
            return False
    if not isinstance(validation.get("passed"), bool):
        return False
    # Reuse evidence is mandatory: prompts/code.md requires the worker to prove
    # it searched for existing equivalents (before and after implementing), and
    # a result that omits the field skipped the check.
    if not isinstance(payload.get("reuse"), str):
        return False
    return isinstance(payload.get("remaining_risks"), str)


def _validate_test_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("command"), str):
        return False
    for key in ("passed", "failed"):
        value = payload.get(key)
        # A negative count is nonsense; accepting it let a malformed result
        # classify as a completed run.
        if not _is_int(value) or value < 0:
            return False
    failures = payload.get("failures")
    if not isinstance(failures, list):
        return False
    for failure in failures:
        if not isinstance(failure, dict):
            return False
        if not isinstance(failure.get("test"), str):
            return False
        if not isinstance(failure.get("error"), str):
            return False
    return True


def _validate_review_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("overall"), str):
        return False
    if not payload.get("overall", "").strip():
        # The contract says every string field is non-empty; an empty verdict
        # paragraph would reach the planner as a result with nothing in it.
        return False
    severity_counts = payload.get("severity_counts")
    if not isinstance(severity_counts, dict):
        return False
    for key in ("high", "medium", "low"):
        value = severity_counts.get(key)
        if not _is_int(value) or value < 0:
            return False
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return False
    tally = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        for key in ("severity", "file_line", "summary", "impact", "suggested_fix"):
            value = finding.get(key)
            if not isinstance(value, str) or not value.strip():
                # An empty string is not a value. A finding with no location in
                # particular cannot be checked, so it is not a finding.
                return False
        severity = finding["severity"].strip().lower()
        if severity not in tally:
            return False
        tally[severity] += 1
    # The counts must agree with the list. A worker claiming five high findings
    # while listing none would otherwise reach the planner as a valid result and
    # misreport how serious the review was.
    if any(severity_counts[key] != tally[key] for key in tally):
        return False
    # An unexpected key would otherwise ride along unvalidated and unreported.
    if set(severity_counts) != set(tally):
        return False
    return True


def result_problem(final_text: str, mode: str) -> str | None:
    """One sentence naming why the result block was rejected. None when parsed.

    `parse_state: "malformed"` with `result: null` and nothing else cost a real
    finding: a worker omitted `suggested_fix` from its one finding, the planner
    read "malformed", shrugged, and never opened `final_text_path`. A state with
    no reason attached is a state nobody acts on.

    Reports the SHAPE the worker sent — top-level keys, and the keys of the first
    finding — rather than re-implementing the per-mode rules. Duplicating the
    rules in order to explain them is exactly how two things that must agree
    drift apart, so this says what arrived and lets the reader compare it to the
    contract. It does not name the failing rule.
    """
    begin = final_text.rfind(RESULT_BEGIN)
    if begin == -1:
        return "no PILOT_RESULT block in the final text"
    end = final_text.rfind(RESULT_END)
    content_start = begin + len(RESULT_BEGIN)
    if end == -1 or end < content_start:
        return "PILOT_RESULT block opened but never closed (cut off mid-block)"
    raw = final_text[content_start:end]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (f"the block is not valid JSON: {exc.msg} at line {exc.lineno} "
                f"column {exc.colno}")
    validator = _RESULT_VALIDATORS.get(mode)
    if validator is None:
        return f"no result schema is defined for mode {mode!r}"
    if validator(payload):
        return None
    shape = f"top-level keys: {sorted(payload)}" if isinstance(payload, dict) \
        else f"top level is a {type(payload).__name__}, not an object"
    entries = payload.get("findings") or payload.get("facts") \
        if isinstance(payload, dict) else None
    if isinstance(entries, list) and entries and isinstance(entries[0], dict):
        shape += f"; first entry keys: {sorted(entries[0])}"
    return f"the block is valid JSON but does not match the {mode} schema — {shape}"


_RESULT_VALIDATORS = {
    "explore": _validate_explore_result,
    "code": _validate_code_result,
    "test": _validate_test_result,
    "review": _validate_review_result,
    "discuss": _validate_discuss_result,
    "resume": _validate_code_result,
}


def extract_result(
    final_text: str, mode: str
) -> tuple[str, dict[str, Any] | None]:
    """Extract the structured result block from the final text event.

    Returns ``(parse_state, result)``:

    - ``("parsed", dict)`` — the last RESULT_BEGIN..RESULT_END block holds JSON
      that validates against the per-mode schema.
    - ``("malformed", None)`` — a block IS there and the worker did the work, but
      the JSON does not parse or does not validate. A worker that quotes JSON
      inside a string field ("emitting `"status": "x"`") produces exactly this,
      and one unescaped quote used to be indistinguishable from a worker that
      ignored the contract entirely — so the planner had no way to know that a
      complete report was sitting in ``final_text_path``.
    - ``("unstructured", None)`` — a begin marker with no end marker: the report
      was cut off mid-block (step cap, timeout).
    - ``("unavailable", None)`` — no RESULT_BEGIN marker at all.
    """
    begin = final_text.rfind(RESULT_BEGIN)
    if begin == -1:
        return "unavailable", None
    content_start = begin + len(RESULT_BEGIN)
    end = final_text.rfind(RESULT_END)
    if end == -1 or end < content_start:
        return "unstructured", None
    try:
        payload = json.loads(final_text[content_start:end])
    except json.JSONDecodeError:
        return "malformed", None
    validator = _RESULT_VALIDATORS.get(mode)
    if validator is None or not validator(payload):
        return "malformed", None
    return "parsed", payload


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def classify_verdict(
    parsed: dict[str, Any],
    step_cap: int,
    summary: dict[str, Any] | None,
    parse_state: str,
    child_exit_code: int | None = None,
) -> str:
    """Apply the fixed-order verdict rules; first match wins.

    ``child_exit_code`` is the wrapper process's own exit status, and it is the
    only evidence available when no ``worker_runner.summary`` arrives — a wrapper
    killed after it printed ``started`` (OOM, SIGKILL, a crash inside
    run_process) emits no summary at all. Without it a killed run whose partial
    text happened to reach the length threshold was classified ``completed``.
    """
    final_text = parsed["final_text"]
    if parsed["steps"] >= step_cap:
        return "step_capped_partial"
    if parse_state == "parsed":
        return "completed"
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
        # No summary: the wrapper died before reporting. A non-zero or
        # signal exit (negative on POSIX) is an error however much text arrived.
        if isinstance(child_exit_code, int) and child_exit_code != 0:
            return "error"
        if parsed["has_error_event"] and len(final_text) < EMPTY_FINAL_TEXT_THRESHOLD:
            return "error"
    if len(final_text) >= EMPTY_FINAL_TEXT_THRESHOLD:
        return "completed"
    return "empty"


def build_verdict(
    *,
    run_id: str,
    resume_run_id: str | None = None,
    provider: str | None,
    runner: str | None,
    mode: str,
    parsed: dict[str, Any],
    summary: dict[str, Any] | None,
    jsonl_path: str,
    stderr_path: str | None,
    step_cap: int,
    report_path: str | None = None,
    child_stderr_text: str = "",
    child_exit_code: int | None = None,
) -> dict[str, Any]:
    final_text = parsed["final_text"]
    parse_state, result = extract_result(final_text, mode)
    parse_error = None if parse_state == "parsed" else result_problem(
        final_text, mode)
    verdict = classify_verdict(
        parsed, step_cap, summary, parse_state, child_exit_code)
    if summary is not None:
        exit_code: Any = summary.get("exit_code")
        timed_out = bool(summary.get("timed_out"))
        idle_timed_out = bool(summary.get("idle_timed_out"))
        interrupted = bool(summary.get("interrupted"))
        session_id: Any = summary.get("session_id")
    else:
        # Report the wrapper's status rather than null: "the run failed and we
        # do not know why" is a worse answer than the code it exited with.
        exit_code = child_exit_code
        timed_out = False
        idle_timed_out = False
        interrupted = False
        session_id = parsed.get("session_id")

    # An error/empty verdict is exactly when the planner needs the child's own
    # words, and exactly when the verdict used to make it open two files to get
    # them. fanout's synthesized verdicts already carried this; the real ones
    # did not, so the contract was weakest on its worst cases.
    stderr_tail = ""
    if verdict in ("error", "empty") and child_stderr_text.strip():
        # `run_process` redacts only the key it was handed, so a mention of any
        # OTHER configured key survives in the child's stderr. That was fine
        # while the text stayed in a local 0600 log; putting it in the verdict
        # sends it to the planner, so redact against every key this machine
        # holds before slicing.
        stderr_tail = runtime.redact_secrets(
            child_stderr_text, runtime.configured_secrets())[-STDERR_TAIL_BYTES:]

    return {
        "type": "worker_runner.verdict",
        "schema_version": VERDICT_SCHEMA_VERSION,
        "run_id": run_id,
        # What to pass as --run-id to resume this work. For a resume it differs
        # from run_id (see cli/run.py): reporting only run_id made a second
        # resume impossible and blamed retention for it.
        "resume_run_id": resume_run_id or run_id,
        # Why `result` is null, when it is. Without it a planner reads
        # "malformed" and moves on; with it the shape the worker actually sent is
        # right there in the verdict.
        "parse_error": parse_error,
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
        "parse_state": parse_state,
        "result": result,
        "final_text_path": report_path,
        "jsonl_path": jsonl_path,
        "stderr_path": stderr_path,
        "stderr_tail": stderr_tail,
        "session_id": session_id,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically at 0600.

    One shared implementation (``runtime.atomic_write_text``); this name stays
    because the report/verdict writers below read better with it.
    """
    runtime.atomic_write_text(path, text, mode=0o600)


def write_verdict_file(path: Path, verdict: dict[str, Any]) -> None:
    """Write the verdict JSON to disk atomically with 0600 permissions."""
    _atomic_write_text(path, json.dumps(verdict, ensure_ascii=False) + "\n")


def write_report_file(path: Path, final_text: str) -> None:
    """Write the report markdown verbatim (no added newline), 0600, atomic."""
    _atomic_write_text(path, final_text)


# ---------------------------------------------------------------------------
# Reparse mode
# ---------------------------------------------------------------------------


def run_reparse(jsonl_arg: str, mode: str,
                runner_name: str | None = None) -> int:
    # Resolve the name ONCE: it also goes into the verdict, and a verdict that
    # does not name its runner cannot be reparsed again by anything but guesswork.
    runner_name = runner_name or DEFAULT_RUNNER
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
    # The stem names the FILES; the ids inside the verdict come from splitting
    # it. A resumed attempt's jsonl is `<sandbox>+<attempt>`, so reparsing it
    # reported that whole string as `run_id` — an id no sandbox and no dispatch
    # ever had. Same split the lifecycle tools use.
    artifact_stem = jsonl_path.stem
    sandbox_id, _, attempt_id = artifact_stem.partition("+")
    run_id = attempt_id or sandbox_id
    resume_run_id = sandbox_id
    report_path = jsonl_path.parent / f"{artifact_stem}.report.md"
    report_text = parsed["final_text"] or NO_MODEL_OUTPUT_SENTINEL
    try:
        write_report_file(report_path, report_text)
    except OSError as exc:
        print(f"error: cannot write report file: {exc}", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    # The live path fills stderr_tail from the child it just ran; a reparse has
    # the run's stored stderr log instead. Without this, a reparsed error verdict
    # says "read stderr_tail" (per the doctrine) and hands back an empty string.
    stderr_log = jsonl_path.parent / f"{artifact_stem}.stderr.log"
    stored_stderr = ""
    if stderr_log.is_file():
        try:
            stored_stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stored_stderr = ""
    verdict = build_verdict(
        run_id=run_id,
        # Threaded, finally. The edit that computed this silently failed to wire
        # it: the script used `if count == 1` instead of an assert, so the
        # unmatched anchor was swallowed. All three reviewers found the result.
        resume_run_id=resume_run_id,
        provider=None,
        runner=runner_name,
        mode=mode,
        parsed=parsed,
        summary=None,
        jsonl_path=str(jsonl_path),
        stderr_path=str(stderr_log) if stderr_log.is_file() else None,
        step_cap=policy.STEPS_BY_MODE[mode],
        report_path=str(report_path),
        child_stderr_text=stored_stderr,
    )
    sys.stdout.write(json.dumps(verdict, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Dispatch mode
# ---------------------------------------------------------------------------


def start_epilogue_heartbeat() -> threading.Event:
    """Start a daemon thread that emits a harvesting heartbeat to stderr.

    Returns a ``threading.Event`` the caller sets to stop the loop. The
    thread prints ``f"{HEARTBEAT_LINE_PREFIX} harvesting"`` to stderr every
    ``EPILOGUE_HEARTBEAT_SECONDS`` (read at call time) until the event is set.
    The fanout watchdog counts these lines as activity but filters them out
    of the captured ``stderr_tail``.
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            sys.stderr.write(f"{HEARTBEAT_LINE_PREFIX} harvesting\n")
            sys.stderr.flush()
            stop.wait(EPILOGUE_HEARTBEAT_SECONDS)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop


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


# The env/cwd policy for a child of this tool lives in runtime, next to the
# whitelist it is built from — fanout needs exactly the same policy for the
# dispatch children it spawns, and a private copy per CLI module is how the two
# spawn sites drift apart. Kept as names here because this is where the spawning
# happens and where the tests look.
_child_cwd = runtime.child_cwd
_child_environment = runtime.child_environment



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
    run_id: str | None = None,
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
    # Always a file, never argv: argv is readable by same-user processes via
    # `ps`. ``run_dispatch`` materialises an inline ``--task`` into a private
    # temp file and owns its removal, so this stays a pure builder.
    #
    # `task` is never read. Kept in the signature and asserted rather than
    # removed: eight positional call sites would have to change, and a parameter
    # that silently DROPS what it is handed is the hazard — this turns that into
    # a loud failure and documents why the parameter exists at all.
    assert task is None, (
        "inline task text must be materialised to a file by the caller; "
        "passing it here would drop it silently")
    assert task_file is not None, "task must be materialised to a file first"
    cmd.extend(["--task-file", task_file])
    if session:
        cmd.extend(["--session", session])
    if mode == "resume" and run_id:
        cmd.extend(["--run-id", run_id])
    if worktree:
        cmd.append("--worktree")
    cmd.extend(["--timeout", str(timeout)])
    cmd.extend(["--idle-timeout", str(idle_timeout)])
    return cmd


def run_dispatch(args: argparse.Namespace) -> int:
    """Materialise an inline ``--task`` to a private file, then dispatch.

    The file must not outlive the run on ANY exit — a failed Popen, an interrupt,
    an exception mid-stream. An ExitStack guarantees that; a ``finally`` further
    inside only covered the paths reached after the child had already started.
    """
    task = getattr(args, "task", None)
    if task is None:
        return _run_dispatch_body(args)

    with contextlib.ExitStack() as cleanup:
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".pilot-task.", suffix=".md",
            delete=False,
        )
        # Registered the moment the file EXISTS, before chmod or write: a failure
        # in either still has a file on disk holding part of the task.
        cleanup.callback(_unlink_quietly, handle.name)
        try:
            os.chmod(handle.name, 0o600)
            handle.write(task)
        finally:
            handle.close()

        # argv must not carry the text: hand the child the file instead.
        args.task = None
        args.task_file = handle.name
        return _run_dispatch_body(args)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass



def _run_dispatch_body(args: argparse.Namespace) -> int:
    mode = args.mode
    provider = getattr(args, "provider", None)
    workdir = getattr(args, "workdir", None)
    task = getattr(args, "task", None)
    task_file = getattr(args, "task_file", None)
    session = getattr(args, "session", None)
    run_id = getattr(args, "run_id", None)
    worktree = bool(getattr(args, "worktree", False))
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT_S)
    idle_timeout = getattr(args, "idle_timeout", DEFAULT_IDLE_TIMEOUT_S)

    if getattr(args, "runner", None) is not None:
        print(
            "error: --runner is only valid with --reparse; dispatch mode "
            "determines the runner from the provider",
            file=sys.stderr,
        )
        return DISPATCH_ERROR_EXIT

    _validate_dispatch_args(mode, provider, workdir, task, task_file)
    if mode != "resume" and run_id:
        # Dropped in silence before: `_build_runner_command` forwards --run-id
        # only for resume, and `run.py` rejects it for every other mode anyway,
        # so a planner passing it to a cold dispatch got neither the sandbox it
        # named nor a word about it. Same class as the swallowed --global-key.
        print("error: --run-id is only valid with --mode resume", file=sys.stderr)
        return DISPATCH_ERROR_EXIT
    if mode == "resume" and not run_id:
        raise RuntimeError("--run-id is required when --mode resume is used")

    workdir = str(Path(workdir).expanduser().resolve())
    if task_file is not None:
        task_file = str(Path(task_file).expanduser().resolve())

    cmd = _build_runner_command(
        provider, mode, workdir, None, task_file,
        session, worktree, timeout, idle_timeout, run_id=run_id,
    )

    started: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None
    child_exit_code: int | None = None
    child_stderr_text = ""
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as child_stderr:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=child_stderr,
                text=True,
                bufsize=1,
                cwd=_child_cwd(),
                env=_child_environment(),
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
            child_exit_code = proc.wait()
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

    # Epilogue: heartbeat beats from child-exit through verdict print so the
    # fanout watchdog knows we are still harvesting (JSONL parse + report/
    # verdict writes). Stop happens in finally on every return path.
    heartbeat_stop = start_epilogue_heartbeat()
    try:
        if started is None:
            # No started event has two very different causes: `run` refused
            # before launching anything (a rejected task, a bad workdir), or the
            # runner launched and stayed silent. Naming the runner in the first
            # case sends the reader to the wrong component, so let the child's
            # own stderr speak and only blame the runner when it said nothing.
            if child_stderr_text.strip():
                print(child_stderr_text.rstrip(), file=sys.stderr)
            else:
                print(
                    "error: runner never emitted worker_runner.started "
                    "and wrote nothing to stderr",
                    file=sys.stderr,
                )
            return DISPATCH_ERROR_EXIT

        log_path_str = started.get("log")
        stderr_log_str = started.get("stderr_log")
        run_id = started.get("run_id")
        provider_from_started = started.get("provider")
        runner_name = started.get("runner") or DEFAULT_RUNNER
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
        # From the LOG STEM, so every per-run artifact of a resumed attempt
        # carries the `<sandbox>+<attempt>` prefix the lifecycle tools glob
        # for. Named from the attempt id alone, the report and verdict of a
        # resume were orphaned by the reaper.
        artifact_stem = log_path.stem
        report_path = log_path.parent / f"{artifact_stem}.report.md"
        report_text = parsed["final_text"] or NO_MODEL_OUTPUT_SENTINEL
        try:
            write_report_file(report_path, report_text)
        except OSError as exc:
            print(f"error: cannot write report file: {exc}", file=sys.stderr)
            return DISPATCH_ERROR_EXIT
        verdict = build_verdict(
            run_id=run_id,
            resume_run_id=started.get("resume_run_id"),
            provider=provider_from_started,
            runner=runner_name,
            mode=mode,
            parsed=parsed,
            summary=summary,
            jsonl_path=str(log_path),
            stderr_path=stderr_log_str,
            step_cap=step_cap,
            report_path=str(report_path),
            child_stderr_text=child_stderr_text,
            child_exit_code=child_exit_code,
        )

        verdict_path = log_path.parent / f"{artifact_stem}.verdict.json"
        try:
            write_verdict_file(verdict_path, verdict)
        except OSError as exc:
            print(f"error: cannot write verdict file: {exc}", file=sys.stderr)
            return DISPATCH_ERROR_EXIT

        sys.stdout.write(json.dumps(verdict, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return 0
    finally:
        heartbeat_stop.set()


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
