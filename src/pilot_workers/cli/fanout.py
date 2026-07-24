#!/usr/bin/env python3
"""Concurrent multi-job dispatch with aggregated verdicts.

Takes N pre-written task files, dispatches them concurrently (each via a
``pilot_workers.cli.dispatch`` child process), re-emits each child's
``worker_runner.started`` event on stderr, and prints ONE JSON array of
verdicts on stdout -- one object per job, in the original job order.

Log note: same-provider concurrent jobs interleave that provider's
``latest.log`` (human log only; per-run jsonl/verdict.json files are still
separate).

Stdout contract (callers depend on it): exactly one JSON array, nothing
else. Started events and warnings go to stderr only.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from pilot_workers import policy, providers, runtime
from pilot_workers.runners import get_runner


FANOUT_ERROR_EXIT = 2
DEFAULT_TIMEOUT_S = 3600
DEFAULT_IDLE_TIMEOUT_S = 900
JOBS_FILE_FIELDS = {"provider", "mode", "task_file", "worktree"}
SUCCESS_VERDICTS = ("completed", "step_capped_partial")

# D4 fanout-hardening constants. All timing values are read at call time
# (never bound at import) so tests can monkeypatch them per-test.
HARVEST_ALLOWANCE_SECONDS = 5.0
MAX_EPILOGUE_SECONDS = 30.0
WATCHDOG_POLL_INTERVAL = 0.05
# Heartbeat lines emitted by dispatch.py's epilogue are excluded from the
# captured stderr_tail. Kept as a literal here to avoid a cross-module
# dependency for a single string.
HEARTBEAT_LINE_PREFIX = "pilot-workers-heartbeat"


@dataclass
class Job:
    provider: str
    mode: str
    task_file: str
    worktree: bool = False

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.mode}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dispatch several jobs concurrently; stdout is one JSON array "
            "of verdicts in the original job order."
        )
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Existing project directory passed to every worker.",
    )
    parser.add_argument(
        "--job",
        action="append",
        default=[],
        metavar="PROVIDER:MODE:TASK_FILE",
        help="One job spec; repeatable. Parsed with a left split (paths may "
             "contain colons).",
    )
    parser.add_argument(
        "--jobs-file",
        default=None,
        metavar="JSON",
        help="JSON array of job objects: provider, mode, task_file, worktree.",
    )
    parser.add_argument(
        "--providers",
        default=None,
        metavar="KEYS",
        help="Comma-separated provider keys (shorthand; requires --mode and "
             "--task-file).",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help="Worker mode for the --providers shorthand.",
    )
    parser.add_argument(
        "--task-file",
        default=None,
        help="Shared task file for the --providers shorthand.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        metavar="N",
        help="Maximum concurrent jobs (default: number of jobs).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Per-job wall-clock limit in seconds (default {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=DEFAULT_IDLE_TIMEOUT_S,
        help=f"Per-job idle limit in seconds (default {DEFAULT_IDLE_TIMEOUT_S}).",
    )
    parsed = parser.parse_args(argv)
    if parsed.max_parallel is not None and parsed.max_parallel < 1:
        parser.error("--max-parallel must be >= 1")
    return parsed


# ---------------------------------------------------------------------------
# Job list construction / validation
# ---------------------------------------------------------------------------


class _SpecError(RuntimeError):
    pass


def _validate_job(job: Job) -> None:
    if job.provider not in providers.PROVIDERS:
        raise _SpecError(f"unknown provider: {job.provider}")
    if job.mode not in policy.MODE_TO_AGENT:
        raise _SpecError(f"unknown mode: {job.mode}")
    if job.mode == "resume":
        raise _SpecError("resume is not supported in fanout")
    if not Path(job.task_file).is_file():
        raise _SpecError(f"task file not found: {job.task_file}")


def _parse_job_spec(spec: str) -> Job:
    parts = spec.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise _SpecError(f"invalid --job spec (expected PROVIDER:MODE:TASK_FILE): {spec}")
    return Job(provider=parts[0], mode=parts[1], task_file=parts[2])


def _load_jobs_file(path_arg: str) -> list[Job]:
    try:
        with open(path_arg, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise _SpecError(f"cannot read jobs file: {exc}")
    if not isinstance(data, list):
        raise _SpecError("jobs file must be a JSON array of job objects")
    jobs: list[Job] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise _SpecError("jobs file entries must be objects")
        unknown = set(entry) - JOBS_FILE_FIELDS
        if unknown:
            raise _SpecError(
                f"unknown jobs file fields: {', '.join(sorted(unknown))}"
            )
        missing = {"provider", "mode", "task_file"} - set(entry)
        if missing:
            raise _SpecError(
                f"jobs file entry missing fields: {', '.join(sorted(missing))}"
            )
        jobs.append(
            Job(
                provider=str(entry["provider"]),
                mode=str(entry["mode"]),
                task_file=str(entry["task_file"]),
                worktree=bool(entry.get("worktree", False)),
            )
        )
    return jobs


def _build_jobs(args: argparse.Namespace) -> list[Job]:
    shorthand_used = any([args.providers, args.mode, args.task_file])
    explicit_used = bool(args.job) or args.jobs_file is not None
    if args.job and args.jobs_file is not None:
        raise _SpecError("--job and --jobs-file are mutually exclusive")
    if shorthand_used and explicit_used:
        raise _SpecError(
            "--providers/--mode/--task-file are mutually exclusive with "
            "--job and --jobs-file"
        )
    if shorthand_used:
        if not (args.providers and args.mode and args.task_file):
            raise _SpecError("--providers requires --mode and --task-file")
        jobs = [
            Job(provider=key.strip(), mode=args.mode, task_file=args.task_file)
            for key in args.providers.split(",")
            if key.strip()
        ]
    elif args.jobs_file is not None:
        jobs = _load_jobs_file(args.jobs_file)
    else:
        jobs = [_parse_job_spec(spec) for spec in args.job]
    if not jobs:
        raise _SpecError("at least one job is required")
    for job in jobs:
        _validate_job(job)
    return jobs


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def _credential_preflight(jobs: list[Job]) -> None:
    seen: set[str] = set()
    for job in jobs:
        if job.provider in seen:
            continue
        seen.add(job.provider)
        provider = providers.PROVIDERS[job.provider]
        meta = runtime.credential_metadata(provider, get_runner(provider.runner))
        if not meta.get("configured"):
            raise _SpecError(
                f"credential missing for {job.provider}; "
                f"run: pilot-workers credentials {job.provider}"
            )


def _workdir_collision_guard(jobs: list[Job]) -> None:
    code_jobs = [job for job in jobs if job.mode == "code"]
    if len(code_jobs) >= 2 and not all(job.worktree for job in code_jobs):
        raise _SpecError(
            "multiple code jobs share one workdir; use --jobs-file with "
            'per-job "worktree": true'
        )
    test_jobs = [job for job in jobs if job.mode == "test"]
    if len(test_jobs) >= 2:
        print(
            "note: concurrent test jobs share a workdir; caches may collide",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _build_dispatch_command(
    job: Job, workdir: str, timeout: int, idle_timeout: int
) -> list[str]:
    cmd: list[str] = [
        sys.executable,
        "-m", "pilot_workers.cli.dispatch",
        "--provider",
        job.provider,
        "--mode",
        job.mode,
        "--workdir",
        workdir,
        "--task-file",
        job.task_file,
    ]
    if job.worktree:
        cmd.append("--worktree")
    cmd.extend(["--timeout", str(timeout)])
    cmd.extend(["--idle-timeout", str(idle_timeout)])
    return cmd


def _synthesized_verdict(
    job: Job,
    exit_code: Any,
    stderr_tail: str,
    reason: str = "crash",
) -> dict[str, Any]:
    """Build a full v2-shape synthesized verdict (D3/D4).

    ``reason`` is the killer enum (``crash|timeout|idle_timeout|interrupted``);
    it is the only field consumers read on a synthesized verdict. Real child
    verdicts are never produced by this function.
    """
    return {
        "job": job.label,
        "type": "worker_runner.verdict",
        "schema_version": 2,
        "verdict": "error",
        "synthesized": True,
        "reason": reason,
        "exit_code": exit_code,
        "stderr_tail": stderr_tail,
        "parse_state": "unavailable",
        "result": None,
        "final_text_path": None,
    }


def _compute_deadline(timeout_s: int) -> float | None:
    """Deadline = timeout + grace + harvest allowance, or None if unbounded.

    All timing constants are read at call time so tests can monkeypatch them
    per-test (the import-time value is never bound).
    """
    if timeout_s == 0:
        return None
    return (
        timeout_s
        + runtime.TERMINATE_GRACE_SECONDS
        + HARVEST_ALLOWANCE_SECONDS
    )


def _kill_job_group(proc: Any) -> None:
    """SIGTERM the child's process group, wait the grace period, then SIGKILL.

    The child was spawned with ``start_new_session=True`` so its PID equals its
    PGID. ``ProcessLookupError`` is swallowed (race between poll and kill).
    Best-effort: never raises.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return
    try:
        proc.wait(timeout=runtime.TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    except Exception:
        pass


def _watchdog_job(
    index: int,
    proc: Any,
    deadline: float,
    last_activity: list[float],
    reasons: list[str | None],
) -> None:
    """Per-job watchdog thread. Polls read-only; never touches pipes.

    After ``deadline``: kill when stdout/stderr silent >=
    ``runtime.TERMINATE_GRACE_SECONDS``. Absolute ceiling
    ``deadline + MAX_EPILOGUE_SECONDS`` kills regardless of activity.
    ``reasons[index]`` is set to ``"timeout"`` BEFORE the kill.
    """
    absolute_ceiling = deadline + MAX_EPILOGUE_SECONDS
    while True:
        if proc.poll() is not None:
            return
        now = time.monotonic()
        if now >= absolute_ceiling:
            if reasons[index] is None:
                reasons[index] = "timeout"
            _kill_job_group(proc)
            return
        if now >= deadline:
            silence = now - last_activity[index]
            if silence >= runtime.TERMINATE_GRACE_SECONDS:
                if reasons[index] is None:
                    reasons[index] = "timeout"
                _kill_job_group(proc)
                return
        time.sleep(WATCHDOG_POLL_INTERVAL)


def _install_signal_handlers(
    procs: list[Any],
    reasons: list[str | None],
) -> tuple[list[bool], dict[int, Any]]:
    """Install SIGINT+SIGTERM handlers that record reason then killpg each
    tracked running child group.

    On the first SIGINT the handler sets the shared interrupted flag, writes
    ``reasons[i] = "interrupted"`` for every still-running tracked job (BEFORE
    killing), and SIGTERM/SIGKILL-reaps the group. A second SIGINT restores
    ``signal.SIG_DFL`` (hard-kill escape). SIGTERM uses the same body but
    never escalates the escape. Handlers return (no ``sys.exit``) so the
    one-JSON-array stdout contract holds.

    Returns ``(interrupted_flag, previous_handlers)`` so the caller can read
    the flag and restore the previous handlers in a ``finally``.
    """
    interrupted = [False]
    state = {"sigint_seen": False}
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }

    def _handler(signum: int, frame: Any) -> None:
        # Second SIGINT restores SIG_DFL (hard-kill escape hatch).
        if signum == signal.SIGINT:
            if state["sigint_seen"]:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                return
            state["sigint_seen"] = True
        interrupted[0] = True
        # WRITE ORDER INVARIANT: reason is recorded BEFORE any killpg.
        for i, proc in enumerate(procs):
            if proc is None:
                continue
            try:
                if proc.poll() is not None:
                    continue
            except Exception:
                continue
            if reasons[i] is None:
                reasons[i] = "interrupted"
            _kill_job_group(proc)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    return interrupted, previous


def _run_job(
    index: int,
    job: Job,
    cmd: list[str],
    timeout_s: int,
    results: list[dict[str, Any] | None],
    procs: list[Any],
    reasons: list[str | None],
    last_activity: list[float],
    interrupted: list[bool],
    stderr_lock: threading.Lock,
) -> None:
    """Run one dispatch child; never raises out -- always records a verdict.

    Single-writer rule: this is the ONLY writer of ``results[index]`` while
    the pool is alive. Killers (signal handler, watchdog) only touch
    ``reasons[index]`` and the proc itself.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        procs[index] = proc
        spawn_time = time.monotonic()
        last_activity[index] = spawn_time

        # Spawn race: if a signal arrived between Popen and procs[index]=proc,
        # the handler could not see this proc. Re-check and kill immediately.
        if interrupted[0]:
            if reasons[index] is None:
                reasons[index] = "interrupted"
            _kill_job_group(proc)

        # _compute_deadline returns the offset; anchor at spawn_time so the
        # watchdog compares against time.monotonic() in the same frame.
        deadline_offset = _compute_deadline(timeout_s)
        deadline = (
            spawn_time + deadline_offset if deadline_offset is not None else None
        )

        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            try:
                assert proc is not None and proc.stderr is not None
                for raw in proc.stderr:
                    # Heartbeat lines AND any stderr line count as activity.
                    last_activity[index] = time.monotonic()
                    stderr_chunks.append(raw)
            except Exception:
                pass

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()

        if deadline is not None:
            watchdog = threading.Thread(
                target=_watchdog_job,
                args=(index, proc, deadline, last_activity, reasons),
                daemon=True,
            )
            watchdog.start()

        started_seen = False
        last_line = ""
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            last_activity[index] = time.monotonic()
            if not started_seen:
                started_seen = True
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    with stderr_lock:
                        sys.stderr.write(
                            json.dumps({"job": job.label, **event},
                                       ensure_ascii=False)
                            + "\n"
                        )
                        sys.stderr.flush()
            last_line = line
        proc.wait()
        drain.join(timeout=5)

        # Build stderr_tail: drop heartbeat lines, then take last 500 chars.
        filtered = [
            chunk for chunk in stderr_chunks
            if not chunk.startswith(HEARTBEAT_LINE_PREFIX)
        ]
        stderr_tail = "".join(filtered)[-500:]

        # Reason precedence: a real child verdict line wins verbatim; the
        # recorded reason (if any) is logged to stderr ONLY. The verdict line
        # is identified by type; a captured started event does NOT count.
        verdict: dict[str, Any] | None = None
        if last_line:
            try:
                parsed = json.loads(last_line)
            except json.JSONDecodeError:
                parsed = None
            if (
                isinstance(parsed, dict)
                and parsed.get("type") == "worker_runner.verdict"
            ):
                parsed["job"] = job.label
                verdict = parsed
        # Log the recorded reason to stderr regardless of whether the real
        # verdict wins: consumers read reason only on synthesized verdicts,
        # but the operator gets the diagnostic either way.
        if reasons[index] is not None:
            with stderr_lock:
                sys.stderr.write(
                    f"fanout: {job.label} recorded reason: "
                    f"{reasons[index]}\n"
                )
                sys.stderr.flush()
        if verdict is None:
            reason = reasons[index] or "crash"
            verdict = _synthesized_verdict(
                job, proc.returncode, stderr_tail, reason=reason,
            )
        results[index] = verdict
    except Exception as exc:  # the array must always be printed
        results[index] = _synthesized_verdict(
            job, getattr(proc, "returncode", None), str(exc)[-500:],
        )


def run_fanout(args: argparse.Namespace) -> int:
    if not args.workdir:
        print("error: --workdir is required", file=sys.stderr)
        return FANOUT_ERROR_EXIT
    try:
        jobs = _build_jobs(args)
        _credential_preflight(jobs)
        _workdir_collision_guard(jobs)
    except _SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return FANOUT_ERROR_EXIT

    max_workers = args.max_parallel or len(jobs)
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    procs: list[Any] = [None] * len(jobs)
    reasons: list[str | None] = [None] * len(jobs)
    last_activity: list[float] = [0.0] * len(jobs)
    stderr_lock = threading.Lock()

    interrupted, previous_handlers = _install_signal_handlers(procs, reasons)
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    _run_job,
                    index,
                    job,
                    _build_dispatch_command(
                        job, args.workdir, args.timeout, args.idle_timeout
                    ),
                    args.timeout,
                    results,
                    procs,
                    reasons,
                    last_activity,
                    interrupted,
                    stderr_lock,
                )
                for index, job in enumerate(jobs)
            ]
            for future in futures:
                future.result()
    finally:
        signal.signal(signal.SIGINT, previous_handlers[signal.SIGINT])
        signal.signal(signal.SIGTERM, previous_handlers[signal.SIGTERM])

    # Post-pool single-threaded backfill for never-started/cancelled slots
    # (exempt from the single-writer rule: all _run_job threads have joined).
    for index, job in enumerate(jobs):
        if results[index] is None:
            reason = reasons[index]
            if reason is None:
                reason = "interrupted" if interrupted[0] else "crash"
            results[index] = _synthesized_verdict(job, None, "", reason=reason)

    final: list[dict[str, Any]] = [r for r in results if r is not None]
    sys.stdout.write(json.dumps(final, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    if interrupted[0]:
        return 1
    if all(
        r.get("verdict") in SUCCESS_VERDICTS
        and not r.get("timed_out")
        and not r.get("idle_timed_out")
        and not r.get("interrupted")
        for r in final
    ):
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_fanout(args)


if __name__ == "__main__":
    raise SystemExit(main())
