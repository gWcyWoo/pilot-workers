#!/usr/bin/env python3
"""CLI entry: pilot-workers run — dispatch a worker task."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys

from pilot_workers import fmt_events, policy, providers, runtime
from pilot_workers.runners.opencode import verify_binary


DEFAULT_TIMEOUT_S = 3600
DEFAULT_IDLE_TIMEOUT_S = 900


def load_task(args: argparse.Namespace) -> str:
    if args.task is not None:
        task = args.task
    else:
        path = Path(args.task_file).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"task file does not exist: {path}")
        if path.stat().st_size > providers.MAX_TASK_BYTES:
            raise RuntimeError(f"task file exceeds {providers.MAX_TASK_BYTES} bytes: {path}")
        task = path.read_text(encoding="utf-8")
    if not task.strip():
        raise RuntimeError("task must not be empty")
    if len(task.encode("utf-8")) > providers.MAX_TASK_BYTES:
        raise RuntimeError(f"task exceeds {providers.MAX_TASK_BYTES} bytes")
    return task.strip()


def validate_mode_arguments(args: argparse.Namespace) -> None:
    if args.mode == "resume" and not args.session:
        raise RuntimeError("--session is required when --mode resume is used")
    if args.mode != "resume" and args.session:
        raise RuntimeError("--session is only valid with --mode resume")
    if args.mode == "resume" and args.worktree:
        raise RuntimeError("resume the previously reported work directory; do not create a new worktree")


def dry_run_summary(provider: providers.Provider, mode: str, workdir: Path) -> dict:
    config = policy.build_config(provider, mode)
    paths = providers.profile_paths(provider)
    return {
        "type": "worker_runner.dry_run",
        "provider": provider.key,
        "provider_id": provider.provider_id,
        "endpoint": provider.base_url,
        "model": provider.model,
        "agent": policy.MODE_TO_AGENT[mode],
        "mode": mode,
        "workdir": str(workdir),
        "sharing": config["share"],
        "enabled_providers": config["enabled_providers"],
        "profile": str(paths["root"]),
        "credential": runtime.credential_metadata(provider),
        "runtime": str(providers.runtime_binary()),
        "runtime_present": providers.runtime_binary().is_file(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch a bounded task to an isolated LLM worker.")
    parser.add_argument("--provider", required=True, choices=sorted(providers.PROVIDERS))
    parser.add_argument("--mode", required=True, choices=sorted(policy.MODE_TO_AGENT))
    parser.add_argument("--workdir", required=True, help="Existing project directory.")
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument("--task", help="Short task contract as a string.")
    task_group.add_argument("--task-file", help="UTF-8 file containing the task contract.")
    parser.add_argument("--session", help="Session ID for resume mode.")
    parser.add_argument("--worktree", action="store_true", help="Create a detached worktree from HEAD.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT_S)
    parser.add_argument("--dry-run", action="store_true", help="Show routing metadata without invoking a model.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_mode_arguments(args)
        if args.timeout < 0 or args.idle_timeout < 0:
            raise RuntimeError("--timeout and --idle-timeout must be >= 0")
        provider = providers.PROVIDERS[args.provider]
        workdir = Path(args.workdir).expanduser().resolve()
        if not workdir.is_dir():
            raise RuntimeError(f"work directory does not exist: {workdir}")
        task = load_task(args)

        if args.dry_run:
            print(json.dumps(dry_run_summary(provider, args.mode, workdir), indent=2))
            return 0

        binary = verify_binary()
        secret = runtime.credential_key(provider)
        if args.worktree:
            workdir = runtime.create_detached_worktree(workdir, providers.worktrees_root())

        config = policy.build_config(provider, args.mode)
        env = runtime.build_environment(provider, config)
        logs = providers.logs_root(provider)
        runtime.ensure_private_directory(logs)
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        log_path = logs / f"{run_id}.jsonl"
        stderr_path = logs / f"{run_id}.stderr.log"
        agent = policy.MODE_TO_AGENT[args.mode]
        prompt = f"<worker-task mode=\"{args.mode}\">\n{task}\n</worker-task>"
        command = [
            binary, "--pure", "run",
            "--model", provider.model,
            "--agent", agent,
            "--format", "json",
            "--thinking",
            "--title", f"pilot-worker-{args.mode}-{run_id}",
            "--dir", str(workdir),
        ]
        if args.session:
            command.extend(["--session", args.session])

        try:
            renderer = fmt_events.FmtWriter(logs, provider.key, run_id, os.getpid())
        except Exception as exc:
            print(f"note: live log rendering unavailable ({exc})", file=sys.stderr)
            renderer = None

        started = {
            "type": "worker_runner.started",
            "provider": provider.key,
            "model": provider.model,
            "mode": args.mode,
            "agent": agent,
            "run_id": run_id,
            "workdir": str(workdir),
            "log": str(log_path),
            "stderr_log": str(stderr_path),
            "rendered_log": str(logs / "latest.log") if renderer else None,
            "timeout_s": args.timeout,
            "idle_timeout_s": args.idle_timeout,
        }
        print(json.dumps(started), flush=True)
        if renderer is not None:
            try:
                renderer.write_event(started)
            except Exception as exc:
                print(f"note: live log rendering disabled ({exc})", file=sys.stderr)
                renderer = None

        result = runtime.run_process(
            command, env, prompt, log_path, stderr_path, secret,
            renderer=renderer, timeout_s=args.timeout, idle_timeout_s=args.idle_timeout,
        )
        secret = ""
        summary = {
            "type": "worker_runner.summary",
            "provider": provider.key,
            "model": provider.model,
            "mode": args.mode,
            "agent": agent,
            "run_id": run_id,
            "session_id": result.session_id or args.session,
            "workdir": str(workdir),
            "log": str(log_path),
            "stderr_log": str(stderr_path),
            "rendered_log": started["rendered_log"],
            "timed_out": result.timed_out,
            "idle_timed_out": result.idle_timed_out,
            "interrupted": result.interrupted,
            "exit_code": result.exit_code,
        }
        print(json.dumps(summary))
        if renderer is not None:
            try:
                renderer.write_event(summary)
                renderer.finalize()
            except Exception as exc:
                print(f"note: live log rendering disabled ({exc})", file=sys.stderr)
        return result.exit_code
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
