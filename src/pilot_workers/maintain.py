#!/usr/bin/env python3
"""Explicit, auditable lifecycle tools for worker logs and detached worktrees.

Never silent: every deletion is printed. Never destructive by surprise:
- log cleanup keeps each provider's newest run pair regardless of age;
- worktree removal refuses when the worktree is dirty or holds commits that
  are not reachable from any ref of the main repository (unintegrated work).
There is no force flag on purpose; unintegrated changes are integrated or
discarded by a human with plain git, not by this script.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from pilot_workers import providers, runtime


def _run_pairs(logs_dir: Path) -> list[list[Path]]:
    """Group per-run files (jsonl + stderr + rendered archive + verdict) by run id, newest first."""
    groups: dict[str, list[Path]] = {}
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name == "latest.log":
            continue
        run_id = None
        if name.endswith(".stderr.log"):
            run_id = name[: -len(".stderr.log")]
        elif name.endswith(".jsonl"):
            run_id = name[: -len(".jsonl")]
        elif name.startswith("rendered-") and name.endswith(".log"):
            run_id = name[len("rendered-") : -len(".log")]
        elif name.endswith(".verdict.json"):
            run_id = name[: -len(".verdict.json")]
        elif name.endswith(".report.md"):
            run_id = name[: -len(".report.md")]
        if run_id is None:
            continue
        groups.setdefault(run_id, []).append(path)
    ordered = sorted(
        groups.values(),
        key=lambda paths: max(item.stat().st_mtime for item in paths),
        reverse=True,
    )
    return ordered


def cleanup_logs(older_than_days: int, provider_keys: list[str]) -> int:
    if older_than_days < 1:
        raise RuntimeError("--older-than-days must be >= 1")
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    for key in provider_keys:
        logs_dir = providers.logs_root(providers.PROVIDERS[key])
        if not logs_dir.is_dir():
            print(f"{key}: no log directory, skipping")
            continue
        pairs = _run_pairs(logs_dir)
        for index, paths in enumerate(pairs):
            newest_mtime = max(item.stat().st_mtime for item in paths)
            if index == 0:
                continue  # always keep the newest run for diagnosis
            if newest_mtime >= cutoff:
                continue
            for path in sorted(paths):
                print(f"delete {path}")
                path.unlink()
                removed += 1
    print(f"removed {removed} file(s)")
    return 0


def _reap_sandbox(root: Path) -> int:
    """Delete one run sandbox tree, lstat-walking it bottom-up.

    Symlinks are unlinked as links and NEVER followed (the canonical
    credential, the shared provider cache, and any worker-created symlink
    pointing outside the sandbox all survive); bare shutil.rmtree is
    deliberately not used. Every deletion is printed. Returns the number of
    entries removed.
    """
    if root.is_symlink():
        raise RuntimeError(f"refusing to reap a symlinked sandbox root: {root}")
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        current = Path(dirpath)
        for name in filenames:
            target = current / name
            print(f"delete {target}")
            target.unlink()
            removed += 1
        for name in dirnames:
            target = current / name
            print(f"delete {target}")
            if target.is_symlink():
                target.unlink()
            else:
                target.rmdir()
            removed += 1
    print(f"delete {root}")
    root.rmdir()
    removed += 1
    return removed


def _run_log_files(provider: providers.Provider, run_id: str) -> list[Path]:
    """All log/report artifacts belonging to one run id."""
    logs_dir = providers.logs_root(provider)
    names = [
        f"{run_id}.jsonl",
        f"{run_id}.stderr.log",
        f"rendered-{run_id}.log",
        f"{run_id}.verdict.json",
        f"{run_id}.report.md",
    ]
    return [logs_dir / name for name in names if (logs_dir / name).is_file()]


def cleanup_runs(older_than_days: int, provider_keys: list[str], keep: int = 1) -> int:
    """Reap run sandboxes older than the cutoff plus their log artifacts.

    Keeps each provider's ``keep`` newest sandboxes regardless of age and
    skips any sandbox holding a live (non-stale) run lock.
    """
    if older_than_days < 1:
        raise RuntimeError("--older-than-days must be >= 1")
    if keep < 1:
        raise RuntimeError("--keep must be >= 1")
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    for key in provider_keys:
        provider = providers.PROVIDERS[key]
        sandboxes_root = providers.runs_root(provider)
        if not sandboxes_root.is_dir():
            continue
        sandboxes = sorted(
            (entry for entry in sandboxes_root.iterdir()
             if entry.is_dir() and not entry.is_symlink()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
        for index, sandbox in enumerate(sandboxes):
            if index < keep:
                continue
            if sandbox.stat().st_mtime >= cutoff:
                continue
            lock = runtime.read_run_lock(sandbox)
            if lock is not None and not runtime.lock_is_stale(lock):
                print(f"{key}: {sandbox.name} skipping (live)")
                continue
            removed += _reap_sandbox(sandbox)
            for log_path in _run_log_files(provider, run_id=sandbox.name):
                print(f"delete {log_path}")
                log_path.unlink()
                removed += 1
    print(f"removed {removed} file(s)")
    return 0


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args], text=True, capture_output=True, check=False
    )


def worktree_status(path: Path) -> dict:
    dirty = None
    unintegrated = None
    head = None
    status = _git(path, "status", "--porcelain")
    if status.returncode == 0:
        dirty = bool(status.stdout.strip())
    head_result = _git(path, "rev-parse", "HEAD")
    if head_result.returncode == 0:
        head = head_result.stdout.strip()
    reachable = _git(path, "rev-list", "HEAD", "--not", "--all")
    if reachable.returncode == 0:
        unintegrated = bool(reachable.stdout.strip())
    return {
        "path": str(path),
        "head": head,
        "dirty": dirty,
        "unintegrated_commits": unintegrated,
        "age_days": round((time.time() - path.stat().st_mtime) / 86400, 1),
    }


def list_worktrees() -> int:
    root = providers.worktrees_root()
    if not root.is_dir():
        print("[]")
        return 0
    entries = [worktree_status(path) for path in sorted(root.iterdir()) if path.is_dir()]
    print(json.dumps(entries, indent=2))
    return 0


def remove_worktree(target: str) -> int:
    root = providers.worktrees_root().resolve()
    path = Path(target).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(f"worktree does not exist: {path}")
    if root not in path.parents and path != root:
        raise RuntimeError(f"refusing to touch a path outside {root}: {path}")
    info = worktree_status(path)
    if info["dirty"] is not False:
        raise RuntimeError(
            f"refusing removal: worktree is dirty or unreadable ({path}); "
            "integrate or discard changes with git first"
        )
    if info["unintegrated_commits"] is not False:
        raise RuntimeError(
            f"refusing removal: worktree holds commits unreachable from any ref ({path}); "
            "integrate them first"
        )
    result = _git(path, "worktree", "remove", str(path))
    if result.returncode != 0:
        raise RuntimeError(f"git worktree remove failed: {result.stderr.strip()}")
    print(f"removed {path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Worker log and worktree lifecycle tools.")
    commands = parser.add_subparsers(dest="command", required=True)

    logs = commands.add_parser("logs", help="Delete old per-run logs (never the newest run).")
    logs.add_argument("--older-than-days", type=int, required=True)
    logs.add_argument("--provider", choices=sorted(providers.PROVIDERS), default=None)

    runs = commands.add_parser("runs", help="Delete old run sandboxes plus their logs (keeps the newest per provider).")
    runs.add_argument("--older-than-days", type=int, required=True)
    runs.add_argument("--keep", type=int, default=1)
    runs.add_argument("--provider", choices=sorted(providers.PROVIDERS), default=None)

    worktrees = commands.add_parser("worktrees", help="List or safely remove detached worktrees.")
    actions = worktrees.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="Show every worker worktree with dirty/integration state.")
    remove = actions.add_parser("remove", help="Remove one clean, integrated worktree.")
    remove.add_argument("path")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "logs":
            keys = [args.provider] if args.provider else sorted(providers.PROVIDERS)
            return cleanup_logs(args.older_than_days, keys)
        if args.command == "runs":
            keys = [args.provider] if args.provider else sorted(providers.PROVIDERS)
            return cleanup_runs(args.older_than_days, keys, keep=args.keep)
        if args.action == "list":
            return list_worktrees()
        return remove_worktree(args.path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
