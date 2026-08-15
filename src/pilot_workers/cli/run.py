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

from pilot_workers import fmt_events, policy, providers, runtime, taskguard
from pilot_workers.runners import get_runner


DEFAULT_TIMEOUT_S = 3600
DEFAULT_IDLE_TIMEOUT_S = 900



# Reading every configured key belongs to the layer that owns credential IO;
# `dispatch` needs the same list to redact what it puts in a verdict.
_configured_secrets = runtime.configured_secrets


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
    if args.mode == "resume" and not args.run_id:
        raise RuntimeError("--run-id is required when --mode resume is used")
    if args.mode != "resume" and args.run_id:
        raise RuntimeError("--run-id is only valid with --mode resume")
    if args.mode == "resume":
        # A pure argument error belongs with the other argument checks, not after
        # resolve_binary and credential_key. It used to sit inside main() past
        # both, so a bad --run-id surfaced as "runtime is missing" on a machine
        # that had not installed one. '+' is rejected because the
        # `<sandbox>+<attempt>` artifact naming depends on it never occurring.
        # '+' because the `<sandbox>+<attempt>` artifact naming depends on it
        # never occurring in a run id, and the glob metacharacters because
        # maintain._run_log_files interpolates a run id straight into a glob
        # pattern — an id containing `*` would match another run's files.
        bad = [c for c in "/\\+*?[]" if c in args.run_id]
        if bad or args.run_id.startswith("."):
            raise RuntimeError(
                "invalid --run-id (path separators, a leading dot, '+' and glob "
                f"metacharacters are not allowed): {args.run_id}")


def dry_run_summary(provider: providers.Provider, mode: str, workdir: Path, *, permission_profile: str | None = None) -> dict:
    runner = get_runner(provider.runner)
    config = runner.build_config(provider, mode, permission_profile=permission_profile)
    paths = providers.profile_paths(provider)
    effective_profile = permission_profile or provider.permissions
    bp = runner.binary_path()
    return {
        "type": "worker_runner.dry_run",
        "provider": provider.key,
        "runner": provider.runner,
        "provider_id": provider.provider_id,
        # An oauth provider has no base_url of its own — the engine's built-in
        # integration carries the endpoint. Reporting "" there reads as a
        # missing value rather than an inapplicable one.
        "endpoint": provider.base_url or f"(built into runner {provider.runner})",
        "auth": provider.auth,
        "model": provider.model,
        "agent": policy.MODE_TO_AGENT[mode],
        "mode": mode,
        "workdir": str(workdir),
        "sharing": config["share"],
        "enabled_providers": config["enabled_providers"],
        "permission_profile": effective_profile,
        "profile": str(paths["root"]),
        "credential": runtime.credential_metadata(provider, runner),
        "runtime": str(bp) if bp else None,
        "runtime_present": bp.is_file() if bp else False,
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
    parser.add_argument("--run-id", help="Run ID of the original run (resume mode only).")
    parser.add_argument("--worktree", action="store_true", help="Create a detached worktree from HEAD.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT_S)
    parser.add_argument("--permissions", help="Permission profile name (overrides provider YAML).")
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
        # Before anything is provisioned, resolved or sent: the task goes
        # verbatim to a third-party endpoint, so a secret in it is exfiltrated
        # the moment a worker starts. This runs ahead of runner resolution and
        # sandbox setup, so its refusal is the error the author sees rather than
        # a later one. It does NOT run before credentials are read — building
        # the exact-match list below opens every configured key file, which is
        # the price of scanning for this machine's own secrets.
        taskguard.check_task(task, known_secrets=_configured_secrets())

        if args.dry_run:
            print(json.dumps(dry_run_summary(provider, args.mode, workdir, permission_profile=args.permissions), indent=2))
            return 0

        runner = get_runner(provider.runner)
        binary = runner.resolve_binary()
        secret = runtime.credential_key(provider, runner)
        if args.worktree:
            workdir = runtime.create_detached_worktree(workdir, providers.worktrees_root())

        config = runner.build_config(provider, args.mode, permission_profile=args.permissions)
        logs = providers.logs_root(provider)
        runtime.ensure_private_directory(logs)
        run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        if args.mode == "resume":
            sandbox = providers.run_paths(provider, args.run_id)
            if not sandbox["root"].is_dir():
                raise RuntimeError(
                    "session expired past retention; redispatch cold: "
                    f"{sandbox['root']}"
                )
            runtime.acquire_run_lock(sandbox["root"])
        else:
            sandbox = runtime.provision_run_sandbox(provider, run_id, runner)
        try:
            env = runtime.build_environment(
                provider, runner.runner_environment(provider, config, paths=sandbox),
                paths=sandbox,
            )
            # A resumed attempt's files are named "<sandbox>+<attempt>" so the
            # lifecycle tools can map a log back to the sandbox whose lock guards
            # it. Without the prefix `maintain logs` looked for a sandbox named
            # after the fresh attempt id, never found one, and deleted the logs
            # of a live resume.
            log_stem = f"{args.run_id}+{run_id}" if args.mode == "resume" else run_id
            log_path = logs / f"{log_stem}.jsonl"
            stderr_path = logs / f"{log_stem}.stderr.log"
            agent = policy.MODE_TO_AGENT[args.mode]
            prompt = runner.format_task_input(task, args.mode)
            command = runner.build_command(binary, provider, args.mode, workdir, run_id, args.session)

            try:
                # log_stem, not run_id: the rendered archive is a per-run
                # artifact like the jsonl, and `maintain` globs for it under
                # the same `<sandbox>+<attempt>` convention. Naming it after
                # the attempt alone put 3 of the 5 per-run files outside the
                # convention the other 2 established.
                renderer = fmt_events.FmtWriter(
                    logs, provider.key, log_stem, os.getpid())
            except Exception as exc:
                print(f"note: live log rendering unavailable ({exc})", file=sys.stderr)
                renderer = None

            started = {
                "type": "worker_runner.started",
                "provider": provider.key,
                "runner": provider.runner,
                "model": provider.model,
                "mode": args.mode,
                "agent": agent,
                "run_id": run_id,
                # The id to pass as --run-id to resume THIS work, which for a
                # resume is NOT run_id: a resume has to mint a fresh run_id
                # because open_private_text is O_CREAT|O_EXCL and cannot reopen
                # the original jsonl. Reporting only run_id meant a planner that
                # resumed twice passed an id no sandbox had ever had, and the
                # failure blamed retention ("session expired") while the sandbox
                # sat there under its original name.
                "resume_run_id": args.run_id or run_id,
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
                runner=runner,
            )
            secret = ""
            summary = {
                "type": "worker_runner.summary",
                "provider": provider.key,
                "runner": provider.runner,
                "model": provider.model,
                "mode": args.mode,
                "agent": agent,
                "run_id": run_id,
                "resume_run_id": started["resume_run_id"],
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
            # `run` is the inner primitive: it streams events and writes the
            # log, but the structured result and report.md are `dispatch`'s
            # job. A user who followed a quick start to `run` would otherwise
            # be left holding a summary with no findings in it — so say where
            # the answer actually is.
            print(f"note: for the extracted result and a report.md, run "
                  f"through 'pw9 dispatch' instead, or reparse this log:\n"
                  f"      pw9 dispatch --reparse {log_path} --mode {args.mode}",
                  file=sys.stderr)
            if renderer is not None:
                try:
                    renderer.write_event(summary)
                    renderer.finalize()
                except Exception as exc:
                    print(f"note: live log rendering disabled ({exc})", file=sys.stderr)
            return result.exit_code
        finally:
            runtime.release_run_lock(sandbox["root"])
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
