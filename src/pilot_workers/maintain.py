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


def _mtime(path: Path) -> float:
    """Modification time, or 0.0 if the file is already gone.

    Two documented commands delete the same log files — ``maintain runs`` reaps
    a sandbox and then that run's logs, ``maintain logs`` reaps logs by age — so
    a cron sweep overlapping a manual one finds paths disappearing under it.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _delete(path: Path) -> int:
    """Delete one artifact, tolerating a concurrent sweep that got there first.

    Returns 1 if this call did the deleting, 0 if it was already gone. Without
    this an overlapping sweep raised FileNotFoundError out of the middle of the
    walk and abandoned the rest of the tree: measured at 10 of 12 files left on
    disk, reported as a bare errno line.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    print(f"delete {path}")
    return 1


def _run_pairs(logs_dir: Path) -> list[tuple[str, list[Path]]]:
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
        groups.items(),
        key=lambda pair: max(_mtime(item) for item in pair[1]),
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
        for index, (run_id, paths) in enumerate(pairs):
            newest_mtime = max(_mtime(item) for item in paths)
            if index == 0:
                continue  # always keep the newest run for diagnosis
            if newest_mtime >= cutoff:
                continue
            # Age alone does not prove the run is over: a long-timeout run can
            # go silent past the cutoff while run_process still holds these
            # files open. A live run lock in the run's sandbox vetoes deletion.
            #
            # Read AND delete under the sandbox's guard: reading the lock and
            # then acting on the answer is two steps, and a resume landing
            # between them had the log files of its now-live run deleted. The
            # guard, not the run lock — this caller does not destroy the
            # sandbox, so it must not leave a .lock behind in one that survives.
            # A resumed run's logs are named "<sandbox_id>+<attempt_id>" (see
            # cli/run.py): it mints a fresh run_id for its own log files because
            # open_private_text is O_CREAT|O_EXCL, while its lock stays in the
            # ORIGINAL sandbox. Deriving the sandbox from the whole stem meant
            # the live-run veto could never find a resumed run's sandbox and
            # deleted its logs mid-run. '+' cannot occur in a run id.
            sandbox_root = providers.run_paths(
                providers.PROVIDERS[key], run_id.split("+", 1)[0])["root"]
            if sandbox_root.is_dir():
                with runtime.run_guard_held(sandbox_root):
                    lock = runtime.read_run_lock(sandbox_root)
                    if lock is not None and not runtime.lock_is_stale(lock):
                        print(f"{key}: {run_id} skipping logs (live run)")
                        continue
                    for path in sorted(paths):
                        removed += _delete(path)
                continue
            # No sandbox left to guard: the run is long over.
            for path in sorted(paths):
                removed += _delete(path)
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
    # The lock guard lives beside the sandbox, not in it (see
    # runtime.run_guard_path), so it needs removing here or guards accumulate
    # in runs_root forever.
    # Through _delete like every other removal here: is_file()-then-unlink is
    # the same check-then-act the rest of this module stopped doing, and the
    # ownerless-guard sweep in cleanup_runs can win the race between the two.
    removed += _delete(runtime.run_guard_path(root))
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
    found = [logs_dir / name for name in names if (logs_dir / name).is_file()]
    # Resumed attempts of THIS sandbox are named "<run_id>+<attempt>"; without
    # them the reaper removes the sandbox and orphans the resume's logs.
    if logs_dir.is_dir():
        found += sorted(p for p in logs_dir.glob(f"{run_id}+*") if p.is_file())
        found += sorted(p for p in logs_dir.glob(f"rendered-{run_id}+*")
                        if p.is_file())
    return found


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
            key=_mtime,
            reverse=True,
        )
        for index, sandbox in enumerate(sandboxes):
            if index < keep:
                continue
            # Both reads go through _mtime. The is_dir() filter above protects
            # only a sandbox that vanishes BEFORE the listing; one reaped by a
            # concurrent sweep after it still aborted the whole sweep here — one
            # line below the read a test had already pinned as safe.
            mtime = _mtime(sandbox)
            if mtime == 0.0 and not sandbox.is_dir():
                continue
            if mtime >= cutoff:
                continue
            # Acquire the run lock rather than merely reading it: a resume can
            # re-lock a stale sandbox between a read-only staleness check and
            # the reap, and would then have its live sandbox deleted under it.
            # Holding the lock makes it the single arbiter for both sides; the
            # reap removes the whole tree, lock included, so no release step.
            try:
                # The lock must be HELD across the whole reap, not merely taken:
                # _reap_sandbox deletes `.lock` early in its walk, and from that
                # moment a resume could acquire the sandbox being deleted.
                # OSError is caught too — a concurrent cleanup can reap this
                # sandbox between iterdir and here, and one such race must not
                # abort the rest of the sweep.
                with runtime.run_lock_held(sandbox):
                    removed += _reap_sandbox(sandbox)
            except (OSError, RuntimeError) as exc:
                print(f"{key}: {sandbox.name} skipping (not reapable: {exc})")
                continue
            # Outside the run lock — the sandbox and its lock are gone by now —
            # so a concurrent `maintain logs` can own these paths already.
            for log_path in _run_log_files(provider, run_id=sandbox.name):
                removed += _delete(log_path)
        # A lock guard outlives its sandbox whenever the sandbox never became a
        # directory — a provision that failed after the lock attempt leaves one
        # behind, and the sweep above only walks directories, so nothing would
        # ever collect it. Zero-byte files, but an unbounded set of them.
        for guard in sandboxes_root.glob(".*.lock.guard"):
            owner = sandboxes_root / guard.name[1:-len(".lock.guard")]
            if owner.is_dir():
                continue
            removed += _delete(guard)
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
    # `--not --all` can NEVER report anything here: git includes every
    # worktree's own HEAD in `--all`, so HEAD excludes itself and the answer is
    # always empty — the refusal below could not fire, and a detached worktree
    # holding the worker's only copy of a commit was removed without complaint.
    # Verified: with a commit made in a detached worktree, `--not --all` prints
    # 0 lines both inside the worktree and from the main repo, while
    # `--not --branches --tags --remotes` prints 1; after the commit is put on a
    # branch it prints 0 again.
    reachable = _git(path, "rev-list", "HEAD",
                     "--not", "--branches", "--tags", "--remotes")
    if reachable.returncode == 0:
        unintegrated = bool(reachable.stdout.strip())
    return {
        "path": str(path),
        "head": head,
        "dirty": dirty,
        "unintegrated_commits": unintegrated,
        # Through _mtime, and None when the worktree is already gone: a
        # concurrent removal between the listing and this read otherwise
        # aborted `maintain worktrees list` with a bare errno. None is
        # this function's own convention for "could not determine".
        "age_days": (round((time.time() - mtime) / 86400, 1)
                     if (mtime := _mtime(path)) else None),
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
