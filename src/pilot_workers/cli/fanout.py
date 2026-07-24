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
    return parser.parse_args(argv)


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
    job: Job, exit_code: Any, stderr_tail: str, interrupted: bool = False
) -> dict[str, Any]:
    verdict: dict[str, Any] = {
        "job": job.label,
        "type": "worker_runner.verdict",
        "verdict": "error",
        "synthesized": True,
        "exit_code": exit_code,
        "stderr_tail": stderr_tail,
    }
    if interrupted:
        verdict["interrupted"] = True
    return verdict


def _run_job(
    index: int,
    job: Job,
    cmd: list[str],
    results: list[dict[str, Any] | None],
    procs: list[Any],
    stderr_lock: threading.Lock,
) -> None:
    """Run one dispatch child; never raises out -- always records a verdict."""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        procs[index] = proc

        stderr_chunks: list[str] = []

        def _drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                stderr_chunks.append(proc.stderr.read())
            except Exception:
                pass

        drain = threading.Thread(target=_drain_stderr, daemon=True)
        drain.start()

        started_seen = False
        last_line = ""
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
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
        stderr_text = "".join(stderr_chunks)

        verdict: dict[str, Any] | None = None
        if last_line:
            try:
                parsed = json.loads(last_line)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                parsed["job"] = job.label
                verdict = parsed
        if verdict is None:
            verdict = _synthesized_verdict(
                job, proc.returncode, stderr_text[-500:]
            )
        results[index] = verdict
    except Exception as exc:  # the array must always be printed
        results[index] = _synthesized_verdict(
            job, getattr(proc, "returncode", None), str(exc)[-500:]
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
    stderr_lock = threading.Lock()
    interrupted = False

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
                    results,
                    procs,
                    stderr_lock,
                )
                for index, job in enumerate(jobs)
            ]
            for future in futures:
                future.result()
    except KeyboardInterrupt:
        interrupted = True
        # Children received SIGINT via the process group and self-terminate;
        # give them a bounded grace period to reap.
        deadline = time.monotonic() + runtime.TERMINATE_GRACE_SECONDS + 5
        for proc in procs:
            if proc is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                proc.wait(timeout=remaining)
            except Exception:
                pass

    for index, job in enumerate(jobs):
        if results[index] is None:
            results[index] = _synthesized_verdict(job, None, "", interrupted)

    final: list[dict[str, Any]] = [r for r in results if r is not None]
    sys.stdout.write(json.dumps(final, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    if interrupted:
        return 1
    if all(r.get("verdict") in SUCCESS_VERDICTS for r in final):
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_fanout(args)


if __name__ == "__main__":
    raise SystemExit(main())
