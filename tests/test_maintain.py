"""RED tests for pilot_workers.maintain D5 additions.

Covers:
- _run_pairs recognizing ``<run_id>.report.md``
- _reap_sandbox lstat-walk survival guarantees (symlinks are unlinked as
  links, never followed; canonical credentials and the shared cache survive)
- the ``maintain runs`` subcommand (retention, live-lock skip, stale-lock
  reap, same-run_id log/report file reaping, one printed line per deletion)

All tests are offline and tmp_path-isolated via PILOT_WORKERS_HOME.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from pilot_workers import maintain, providers, runtime


PROVIDER_KEY = "glm"
OTHER_PROVIDER_KEY = "kimi-k3"


def _dead_pid() -> int:
    """Pid of a real short-lived child that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _populate_sandbox(root):
    (root / "config").mkdir(parents=True)
    (root / "data" / "opencode").mkdir(parents=True)
    (root / "state").mkdir()
    (root / "config" / "settings.json").write_text("{}", encoding="utf-8")
    (root / "data" / "opencode" / "opencode.db").write_text("db", encoding="utf-8")


def _make_run(provider, run_id, *, age_days=0, lock=None):
    """Create a minimal sandbox dir under runs_root; set its mtime last."""
    root = providers.runs_root(provider) / run_id
    _populate_sandbox(root)
    if lock is not None:
        (root / ".lock").write_text(json.dumps(lock), encoding="utf-8")
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(root, (old, old))
    return root


def _make_log_files(provider, run_id):
    logs = providers.logs_root(provider)
    logs.mkdir(parents=True, exist_ok=True)
    names = [
        f"{run_id}.jsonl",
        f"{run_id}.stderr.log",
        f"rendered-{run_id}.log",
        f"{run_id}.verdict.json",
        f"{run_id}.report.md",
    ]
    paths = []
    for name in names:
        path = logs / name
        path.write_text("x", encoding="utf-8")
        paths.append(path)
    return paths


def _maintain_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["maintain", *argv])
    return maintain.main()


# ---------------------------------------------------------------------------
# _run_pairs: .report.md joins the per-run group
# ---------------------------------------------------------------------------


def test_run_pairs_recognizes_report_md(tmp_path):
    (tmp_path / "r1.jsonl").write_text("x", encoding="utf-8")
    (tmp_path / "r1.report.md").write_text("x", encoding="utf-8")
    groups = maintain._run_pairs(tmp_path)
    group = next(paths for run_id, paths in groups if run_id == "r1")
    assert {p.name for p in group} == {"r1.jsonl", "r1.report.md"}


# ---------------------------------------------------------------------------
# _reap_sandbox survival suite
# ---------------------------------------------------------------------------


def test_reap_sandbox_preserves_canonical_auth_json(tmp_path):
    canonical = tmp_path / "canonical" / "auth.json"
    canonical.parent.mkdir()
    canonical.write_text("secret", encoding="utf-8")
    root = tmp_path / "sandbox"
    _populate_sandbox(root)
    os.symlink(str(canonical), str(root / "data" / "opencode" / "auth.json"))

    removed = maintain._reap_sandbox(root)

    assert canonical.read_text(encoding="utf-8") == "secret"
    assert not root.exists()
    assert isinstance(removed, int)


def test_reap_sandbox_preserves_shared_cache(tmp_path):
    shared = tmp_path / "shared-cache"
    shared.mkdir()
    (shared / "blob.bin").write_text("cached", encoding="utf-8")
    root = tmp_path / "sandbox"
    _populate_sandbox(root)
    os.symlink(str(shared), str(root / "cache"))

    maintain._reap_sandbox(root)

    assert (shared / "blob.bin").read_text(encoding="utf-8") == "cached"
    assert not root.exists()


def test_reap_sandbox_preserves_arbitrary_outside_symlink(tmp_path):
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep", encoding="utf-8")
    root = tmp_path / "sandbox"
    _populate_sandbox(root)
    # A worker-created symlink inside the sandbox pointing outside it.
    os.symlink(str(outside), str(root / "state" / "escape"))

    maintain._reap_sandbox(root)

    assert (outside / "sub" / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not root.exists()


def test_reap_sandbox_removes_regular_tree_and_returns_count(tmp_path):
    root = tmp_path / "sandbox"
    _populate_sandbox(root)

    removed = maintain._reap_sandbox(root)

    assert not root.exists()
    assert isinstance(removed, int)
    assert removed >= 2  # settings.json + opencode.db at minimum


# ---------------------------------------------------------------------------
# maintain runs — parse args
# ---------------------------------------------------------------------------


def test_parse_args_runs_defaults(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["maintain", "runs", "--older-than-days", "7"])
    args = maintain.parse_args()
    assert args.command == "runs"
    assert args.older_than_days == 7
    assert args.keep == 1
    assert args.provider is None


def test_parse_args_runs_keep_and_provider(monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["maintain", "runs", "--older-than-days", "7",
         "--keep", "3", "--provider", PROVIDER_KEY])
    args = maintain.parse_args()
    assert args.keep == 3
    assert args.provider == PROVIDER_KEY


# ---------------------------------------------------------------------------
# maintain runs — cleanup behavior
# ---------------------------------------------------------------------------


def test_runs_cleanup_keeps_newest_regardless_of_age(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    oldest = _make_run(provider, "oldest-run", age_days=40)
    newest = _make_run(provider, "newest-run", age_days=30)

    rc = _maintain_main(monkeypatch, "runs", "--older-than-days", "7")

    assert rc == 0
    assert not oldest.exists()
    # Both are older than the cutoff; the newest survives via keep=1.
    assert newest.exists()


def test_runs_cleanup_keep_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    run_50 = _make_run(provider, "run-50", age_days=50)
    run_40 = _make_run(provider, "run-40", age_days=40)
    run_30 = _make_run(provider, "run-30", age_days=30)

    rc = _maintain_main(
        monkeypatch, "runs", "--older-than-days", "7", "--keep", "2")

    assert rc == 0
    assert not run_50.exists()
    assert run_40.exists()
    assert run_30.exists()


def test_runs_cleanup_skips_live_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    live_lock = {
        "pid": os.getpid(),
        "started_at": runtime.process_start_time(os.getpid()),
    }
    live = _make_run(provider, "live-run", age_days=30, lock=live_lock)
    newest = _make_run(provider, "newest-run", age_days=10)

    rc = _maintain_main(monkeypatch, "runs", "--older-than-days", "7")

    assert rc == 0
    assert live.exists()
    assert newest.exists()
    out = capsys.readouterr().out
    assert "live-run skipping" in out
    # The reason is printed, not just the fact: "still active" names the lock.
    assert "still active" in out


def test_runs_cleanup_reaps_stale_lock(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    stale_lock = {"pid": _dead_pid(), "started_at": "whatever"}
    stale = _make_run(provider, "stale-run", age_days=30, lock=stale_lock)
    newest = _make_run(provider, "newest-run", age_days=10)

    rc = _maintain_main(monkeypatch, "runs", "--older-than-days", "7")

    assert rc == 0
    assert not stale.exists()
    assert newest.exists()


def test_runs_cleanup_holds_the_lock_it_reaps_under(tmp_path, monkeypatch, capsys):
    """The reaper must ACQUIRE the run lock, not merely read it.

    A read-only staleness check races a resume that re-locks the sandbox
    between the check and the reap; acquisition makes the lock the single
    arbiter. Simulated by an acquire that reports the lock as just taken.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    stale_lock = {"pid": _dead_pid(), "started_at": "whatever"}
    relocked = _make_run(provider, "relocked-run", age_days=30, lock=stale_lock)
    _make_run(provider, "newest-run", age_days=10)

    @contextlib.contextmanager
    def refuse(root):
        raise RuntimeError(f"run is still active: {root}")
        yield  # pragma: no cover

    monkeypatch.setattr(runtime, "run_lock_held", refuse)
    rc = _maintain_main(monkeypatch, "runs", "--older-than-days", "7")

    assert rc == 0
    # Looks stale on a read, but acquisition failed -> must survive.
    assert relocked.exists()
    assert "relocked-run skipping" in capsys.readouterr().out


def test_a_refused_lock_check_does_not_refresh_the_sandbox_mtime(tmp_path, monkeypatch):
    """Retention and the keep-set order are read from the sandbox mtime.

    The reaper now tries to ACQUIRE each candidate's lock. With the guard file
    created inside the sandbox, even a refused attempt bumped that mtime — so
    looking at a live sandbox postponed its reaping by a whole extra retention
    window, and skewed which sandboxes fill the keep slots.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    live_lock = {
        "pid": os.getpid(),
        "started_at": runtime.process_start_time(os.getpid()),
    }
    sandbox = _make_run(provider, "some-run", age_days=30, lock=live_lock)
    before = sandbox.stat().st_mtime

    with pytest.raises(RuntimeError, match="still active"):
        runtime.acquire_run_lock(sandbox)

    assert sandbox.stat().st_mtime == before, "the lock guard landed inside the sandbox"
    assert runtime.run_guard_path(sandbox).is_file()


def test_reaping_removes_the_lock_guard_too(tmp_path, monkeypatch, capsys):
    """The guard lives beside the sandbox, so the reaper owns its cleanup."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    sandbox = _make_run(provider, "old-run", age_days=30)
    _make_run(provider, "newest-run", age_days=10)
    guard = runtime.run_guard_path(sandbox)
    guard.write_text("", encoding="utf-8")

    assert _maintain_main(monkeypatch, "runs", "--older-than-days", "7") == 0

    assert not sandbox.exists()
    assert not guard.exists(), "the lock guard outlived its sandbox"


def test_a_resume_cannot_acquire_a_sandbox_mid_reap(tmp_path, monkeypatch, capsys):
    """Taking the lock is not enough: the reap DELETES it.

    `.lock` goes early in the bottom-up walk, and from that instant an acquirer
    saw a lock-free sandbox — so a resume could start inside a directory being
    deleted, and the reaper's final rmdir would fail on the files resume
    recreated. The guard flock must be held across the whole reap.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    _make_run(provider, "old-run", age_days=30)
    _make_run(provider, "newest-run", age_days=1)

    acquired: list[bool] = []
    real_reap = maintain._reap_sandbox

    def watched_reap(root):
        def contender():
            try:
                runtime.acquire_run_lock(root)
                acquired.append(True)
            except (OSError, RuntimeError):
                acquired.append(False)

        thread = threading.Thread(target=contender)
        thread.start()
        # Long enough that an unguarded acquirer would have finished.
        thread.join(timeout=0.5)
        mid_reap_winner = bool(acquired)
        result = real_reap(root)
        thread.join(timeout=5)
        assert not mid_reap_winner, (
            "an acquirer got the sandbox while it was being deleted")
        return result

    monkeypatch.setattr(maintain, "_reap_sandbox", watched_reap)
    assert _maintain_main(monkeypatch, "runs", "--older-than-days", "7") == 0


def test_a_guard_whose_sandbox_never_existed_is_collected(tmp_path, monkeypatch, capsys):
    """The guard lives beside the sandbox, so a provision that failed after the
    lock attempt leaves one with nothing to reap it — the sweep walks
    directories only, so they would accumulate forever."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    live = _make_run(provider, "kept-run", age_days=1)
    live_guard = runtime.run_guard_path(live)
    live_guard.write_text("", encoding="utf-8")
    orphan = runtime.run_guard_path(providers.runs_root(provider) / "never-existed")
    orphan.write_text("", encoding="utf-8")

    assert _maintain_main(monkeypatch, "runs", "--older-than-days", "7") == 0

    assert not orphan.exists(), "an ownerless lock guard survived the sweep"
    assert live_guard.exists(), "the guard of a live sandbox was collected"


def test_an_unreapable_sandbox_does_not_abort_the_sweep(tmp_path, monkeypatch, capsys):
    """cleanup_runs caught only RuntimeError, but the guard open raises OSError
    (a concurrent cleanup reaping this sandbox between iterdir and here). One
    such race must not leave every later sandbox and provider unprocessed."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    doomed = _make_run(provider, "aaa-vanishes", age_days=40)
    later = _make_run(provider, "bbb-reapable", age_days=39)
    _make_run(provider, "newest-run", age_days=1)
    real_held = runtime.run_lock_held

    @contextlib.contextmanager
    def held(root):
        if root.name == "aaa-vanishes":
            raise OSError(2, "No such file or directory")
        with real_held(root):
            yield

    monkeypatch.setattr(runtime, "run_lock_held", held)
    assert _maintain_main(monkeypatch, "runs", "--older-than-days", "7") == 0

    assert doomed.exists()
    assert not later.exists(), "the sweep stopped at the first OSError"


def test_logs_cleanup_keeps_files_of_a_live_run(tmp_path, monkeypatch, capsys):
    """Old-by-mtime log files of a run whose sandbox holds a live lock stay.

    A long-timeout run can go silent past the cutoff while run_process still
    has its jsonl/stderr handles open; deleting them fails the harvest.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    live_lock = {
        "pid": os.getpid(),
        "started_at": runtime.process_start_time(os.getpid()),
    }
    _make_run(provider, "quiet-live-run", lock=live_lock)
    live_logs = _make_log_files(provider, "quiet-live-run")
    dead_logs = _make_log_files(provider, "finished-run")
    newest = _make_log_files(provider, "newest-run")
    old = time.time() - 30 * 86400
    for path in [*live_logs, *dead_logs]:
        os.utime(path, (old, old))

    rc = _maintain_main(monkeypatch, "logs", "--older-than-days", "7")

    assert rc == 0
    assert all(path.exists() for path in live_logs), "live run's logs deleted"
    assert not any(path.exists() for path in dead_logs)
    assert all(path.exists() for path in newest)


def test_runs_cleanup_removes_same_run_id_log_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    sandbox = _make_run(provider, "old-run", age_days=30)
    _make_run(provider, "newest-run", age_days=10)
    log_paths = _make_log_files(provider, "old-run")

    rc = _maintain_main(monkeypatch, "runs", "--older-than-days", "7")

    assert rc == 0
    assert not sandbox.exists()
    out = capsys.readouterr().out
    for path in log_paths:
        assert not path.exists(), path.name
        # Every deletion is printed, one line per path.
        assert str(path) in out


def test_runs_cleanup_provider_filter_leaves_other_providers(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    target = providers.PROVIDERS[PROVIDER_KEY]
    other = providers.PROVIDERS[OTHER_PROVIDER_KEY]
    target_old = _make_run(target, "old-run", age_days=30)
    _make_run(target, "newest-run", age_days=10)
    other_old = _make_run(other, "old-run", age_days=30)
    _make_run(other, "newest-run", age_days=10)

    rc = _maintain_main(
        monkeypatch, "runs", "--older-than-days", "7",
        "--provider", PROVIDER_KEY)

    assert rc == 0
    assert not target_old.exists()
    assert other_old.exists()


# ---------------------------------------------------------------------------
# worktree lifecycle: the refusal that protects a worker's only copy of a commit
# ---------------------------------------------------------------------------


def _repo_with_detached_worktree(tmp_path):
    """A real repo plus a detached worktree, the shape --worktree creates."""
    main = tmp_path / "main"
    main.mkdir()

    def git(*args, cwd=main):
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, check=True)

    git("init", "-q", ".")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (main / "f.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    worktree = tmp_path / "wt"
    git("worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    return main, worktree, git


def test_a_committed_worktree_is_reported_unintegrated(tmp_path):
    """`rev-list HEAD --not --all` can never report anything from a worktree:
    git puts every worktree's own HEAD in `--all`, so HEAD excludes itself. The
    refusal that protects the worker's only copy of a commit never fired."""
    main, worktree, git = _repo_with_detached_worktree(tmp_path)
    (worktree / "new.txt").write_text("work\n", encoding="utf-8")
    git("add", "-A", cwd=worktree)
    git("commit", "-qm", "the worker's commit", cwd=worktree)

    info = maintain.worktree_status(worktree)

    assert info["dirty"] is False, "a committed worktree is not dirty"
    assert info["unintegrated_commits"] is True, (
        "a commit that exists nowhere else was reported as integrated")


def test_removal_refuses_a_worktree_holding_the_only_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    main, worktree, git = _repo_with_detached_worktree(tmp_path)
    (worktree / "new.txt").write_text("work\n", encoding="utf-8")
    git("add", "-A", cwd=worktree)
    git("commit", "-qm", "the worker's commit", cwd=worktree)
    monkeypatch.setattr(providers, "worktrees_root", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="unreachable from any ref"):
        maintain.remove_worktree(str(worktree))
    assert worktree.is_dir(), "the worktree was removed despite the refusal"


def test_an_untouched_worktree_is_not_reported_unintegrated(tmp_path):
    """The check must not refuse every worktree — that would be the same bug
    with the sign flipped, and just as useless."""
    main, worktree, git = _repo_with_detached_worktree(tmp_path)
    assert maintain.worktree_status(worktree)["unintegrated_commits"] is False


def test_integrating_the_work_lifts_the_refusal(tmp_path, monkeypatch):
    """Once the commit is on a branch there is a second copy, so removal is safe.
    Without this the refusal would be a dead end with no escape hatch."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    main, worktree, git = _repo_with_detached_worktree(tmp_path)
    (worktree / "new.txt").write_text("work\n", encoding="utf-8")
    git("add", "-A", cwd=worktree)
    git("commit", "-qm", "the worker's commit", cwd=worktree)
    sha = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    git("branch", "-q", "worker-work", sha)
    monkeypatch.setattr(providers, "worktrees_root", lambda: tmp_path)

    assert maintain.worktree_status(worktree)["unintegrated_commits"] is False
    assert maintain.remove_worktree(str(worktree)) == 0


def test_a_dirty_worktree_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    main, worktree, git = _repo_with_detached_worktree(tmp_path)
    (worktree / "f.txt").write_text("uncommitted edit\n", encoding="utf-8")
    monkeypatch.setattr(providers, "worktrees_root", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="dirty"):
        maintain.remove_worktree(str(worktree))
    assert worktree.is_dir()


# ----------------------------------------------------------------------
# A sweep must survive a concurrent sweep.
#
# `maintain runs` deletes a sandbox AND that run's log files; `maintain logs`
# deletes log files by age. They target the SAME paths, so a cron sweep
# overlapping a manual one hit FileNotFoundError in the middle of the walk and
# abandoned the rest of the tree: 10 of 12 files were still on disk, with only
# a bare errno line to show for it.
# ----------------------------------------------------------------------

def _age_all(paths, days):
    old = time.time() - days * 86400
    for path in paths:
        os.utime(path, (old, old))


@pytest.fixture
def vanishing_unlink(monkeypatch):
    """Delete one artifact out from under the sweep, exactly once."""
    from pathlib import Path

    real = Path.unlink
    state = {"fired": False, "match": None}

    def racing(self, *args, **kwargs):
        if not state["fired"] and state["match"] and state["match"](self):
            state["fired"] = True
            real(self)                      # the other sweep got here first
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", racing)
    return state


def test_log_cleanup_finishes_when_another_sweep_deletes_a_file(
        tmp_path, monkeypatch, vanishing_unlink, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    runs = ["20260101T000000Z-aaaaaaa1", "20260102T000000Z-aaaaaaa2",
            "20260103T000000Z-aaaaaaa3", "20260104T000000Z-aaaaaaa4"]
    for run_id in runs:
        _age_all(_make_log_files(provider, run_id), 40)
    # Keep the newest run alive so the sweep has several runs to walk.
    _age_all(_make_log_files(provider, "20260105T000000Z-newest"), 0)
    vanishing_unlink["match"] = lambda p: p.name.endswith(".stderr.log")

    assert maintain.cleanup_logs(30, [PROVIDER_KEY]) == 0
    logs = providers.logs_root(provider)
    survivors = sorted(p.name for p in logs.iterdir())
    for run_id in runs:
        assert not any(name.startswith(run_id) for name in survivors), (
            f"{run_id} was left behind: {survivors}")


def test_run_cleanup_finishes_when_another_sweep_deletes_a_log(
        tmp_path, monkeypatch, vanishing_unlink):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    runs = ["20260101T000000Z-bbbbbbb1", "20260102T000000Z-bbbbbbb2",
            "20260103T000000Z-bbbbbbb3"]
    for run_id in runs:
        _make_run(provider, run_id, age_days=40)
        _make_log_files(provider, run_id)
    _make_run(provider, "20260105T000000Z-newest")
    # The log deletion in cleanup_runs happens AFTER the sandbox lock is gone,
    # so this is the site a concurrent `maintain logs` can win.
    vanishing_unlink["match"] = lambda p: p.name.endswith(".verdict.json")

    assert maintain.cleanup_runs(30, [PROVIDER_KEY], keep=1) == 0
    for run_id in runs:
        assert not (providers.runs_root(provider) / run_id).exists(), run_id
    logs = providers.logs_root(provider)
    survivors = sorted(p.name for p in logs.iterdir())
    for run_id in runs:
        assert not any(name.startswith(run_id) or name.endswith(f"{run_id}.log")
                       for name in survivors), f"{run_id}: {survivors}"


def test_run_cleanup_finishes_when_a_sandbox_vanishes_before_its_stat(
        tmp_path, monkeypatch):
    """A sandbox reaped by another sweep between the listing and the sort must
    not abort this one.

    It does not today, and the reason is worth pinning: the ``is_dir()`` filter
    runs before the ``stat()`` sort key, so a vanished entry is dropped rather
    than stat-ed. Reordering those two would turn this into the same crash the
    two tests above cover."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    doomed = _make_run(provider, "20260101T000000Z-ccccccc1", age_days=40)
    _make_run(provider, "20260102T000000Z-ccccccc2", age_days=40)
    _make_run(provider, "20260105T000000Z-newest")

    real_iterdir = type(doomed).iterdir

    def iterdir_then_reap(self):
        entries = list(real_iterdir(self))
        if self == providers.runs_root(provider):
            maintain._reap_sandbox(doomed)   # the other sweep won the race
        return iter(entries)

    monkeypatch.setattr(type(doomed), "iterdir", iterdir_then_reap)
    assert maintain.cleanup_runs(30, [PROVIDER_KEY], keep=1) == 0
    assert not (providers.runs_root(provider)
                / "20260102T000000Z-ccccccc2").exists(), (
        "the surviving old sandbox was not reaped")


def test_a_guard_that_vanishes_mid_reap_does_not_abort_the_reap(
        tmp_path, monkeypatch, capsys):
    """The lock guard lives OUTSIDE the sandbox, so two sweeps can both own it.

    `_reap_sandbox` removes the sandbox tree and then its guard; `cleanup_runs`
    separately collects guards whose owner directory is gone. Between the
    `rmdir` and the guard unlink the guard IS ownerless, so the second sweep can
    delete it first — and an `is_file()`-then-`unlink()` pair then raised
    FileNotFoundError out of `_reap_sandbox` AFTER the sandbox was already gone.
    `cleanup_runs` reported that as "skipping (not reapable)" and skipped the
    run's log files, orphaning them.

    Simulated rather than threaded: a guard path that reports it exists and
    fails to unlink is exactly the state the loser of that race observes.
    Verified to discriminate — with `_delete` swapped back for the check-then-act
    pair this test fails.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    sandbox = _make_run(provider, "20260101T000000Z-eeeeeee1", age_days=40)

    class VanishedGuard:
        """Reports itself present, then is not there when unlinked."""

        def __init__(self, path):
            self._path = path

        def __fspath__(self):
            return str(self._path)

        def __str__(self):
            return str(self._path)

        def is_file(self):
            return True

        def unlink(self, missing_ok=False):
            raise FileNotFoundError(2, "No such file or directory",
                                    str(self._path))

    monkeypatch.setattr(
        runtime, "run_guard_path",
        lambda root: VanishedGuard(tmp_path / f".{root.name}.lock.guard"))

    removed = maintain._reap_sandbox(sandbox)

    assert not sandbox.exists(), "the sandbox was not reaped"
    assert removed > 0
    out = capsys.readouterr().out
    assert ".lock.guard" not in out, (
        "a guard that was already gone was reported as deleted")


def test_no_mtime_read_in_this_module_is_unguarded():
    """An enumeration guard, because a behaviour test pinned the wrong line.

    A test already proved a sandbox vanishing BEFORE the listing is filtered
    out. One line below that read, `cleanup_runs` called `sandbox.stat()` again
    inside the loop with nothing protecting it, and a sandbox reaped by a
    concurrent sweep aborted the entire sweep there. No amount of testing the
    guarded read finds the unguarded one; asserting the property over every site
    does.
    """
    import re
    from pathlib import Path

    source = Path(maintain.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"\.stat\(\)\.st_mtime", line)
        and "def _mtime" not in line
        and "return path.stat().st_mtime" not in line
    ]
    assert not offenders, (
        "mtime read outside the _mtime helper: " + "; ".join(offenders))


def test_a_resume_cannot_interleave_with_the_guarded_log_sweep(
        tmp_path, monkeypatch):
    """What the guard actually buys: a resume CANNOT acquire the sandbox while
    the sweep holds the guard.

    The first version of this test created the lock inside the patched
    `read_run_lock` — before the sweep's read returned — so the sweep saw a live
    lock and skipped whether or not the wrap existed. kimi and glm both caught
    that independently. The observable that changes is not "the logs survive"
    (with the guard held the sweep finishes and DOES delete them); it is whether
    a concurrent acquirer can get in at all.
    """
    import threading

    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    run_id = "20260101T000000Z-fffffff1"
    sandbox = _make_run(provider, run_id, age_days=40)
    _age_all(_make_log_files(provider, run_id), 40)
    _age_all(_make_log_files(provider, "20260105T000000Z-newest"), 0)

    inside_window = threading.Event()
    release = threading.Event()
    real_delete = maintain._delete

    def stalling_delete(path):
        if path.name.startswith(run_id):
            inside_window.set()
            release.wait(timeout=5)
        return real_delete(path)

    monkeypatch.setattr(maintain, "_delete", stalling_delete)

    sweep = threading.Thread(target=maintain.cleanup_logs, args=(30, [PROVIDER_KEY]))
    sweep.start()
    # A generous LIVENESS wait, not the property under test: under CPU load
    # (three review workers running) the sweep thread can take a while to
    # reach the delete, and a tight bound here fails for the wrong reason.
    assert inside_window.wait(timeout=60), "the sweep never reached the delete"

    acquired = threading.Event()

    def resume():
        try:
            runtime.acquire_run_lock(sandbox)
            acquired.set()
        except RuntimeError:
            acquired.set()          # refused also counts as "did not interleave"

    resumer = threading.Thread(target=resume)
    resumer.start()
    # The guard is held, so the acquirer must still be blocked.
    got_in = acquired.wait(timeout=1.0)
    release.set()
    sweep.join(timeout=5)
    resumer.join(timeout=5)

    assert not got_in, (
        "a resume acquired the sandbox while the log sweep held its guard")


def test_a_resumed_runs_logs_are_attributed_to_its_own_sandbox(
        tmp_path, monkeypatch, capsys):
    """A resume mints a fresh run_id for its LOG files (open_private_text is
    O_CREAT|O_EXCL, so it cannot reopen the original jsonl) while its lock stays
    in the ORIGINAL sandbox. Deriving the sandbox from the whole log stem meant
    the live-run veto looked for a sandbox that never existed and deleted a live
    resume's logs. kimi found it; the interaction was introduced by the round-12
    resume_run_id split.
    """
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    sandbox_id = "20260101T000000Z-aaaaaaa1"
    attempt_id = "20260102T000000Z-bbbbbbb2"

    live = {"pid": os.getpid(),
            "started_at": runtime.process_start_time(os.getpid())}
    _make_run(provider, sandbox_id, lock=live)
    resumed_logs = _make_log_files(provider, f"{sandbox_id}+{attempt_id}")
    _age_all(resumed_logs, 40)
    _age_all(_make_log_files(provider, "20260105T000000Z-newest"), 0)

    assert maintain.cleanup_logs(30, [PROVIDER_KEY]) == 0

    for path in resumed_logs:
        assert path.is_file(), (
            f"{path.name} was deleted although its sandbox holds a live lock")
    assert "skipping logs (live run)" in capsys.readouterr().out


def test_reaping_a_sandbox_also_removes_its_resumed_attempts_logs(
        tmp_path, monkeypatch):
    """The other half: without the glob the reaper removes the sandbox and
    orphans every resumed attempt's files, which nothing would ever collect."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    provider = providers.PROVIDERS[PROVIDER_KEY]
    sandbox_id = "20260101T000000Z-ccccccc1"
    _make_run(provider, sandbox_id, age_days=40)
    first = _make_log_files(provider, sandbox_id)
    resumed = _make_log_files(provider, f"{sandbox_id}+20260102T000000Z-ddddddd2")
    _make_run(provider, "20260105T000000Z-newest")

    assert maintain.cleanup_runs(30, [PROVIDER_KEY], keep=1) == 0
    for path in [*first, *resumed]:
        assert not path.exists(), f"{path.name} was orphaned by the reap"


def test_every_per_run_artifact_uses_one_naming_convention():
    """The guard that would have caught applying `<sandbox>+<attempt>` to 2 of 5.

    Three reviewers found the same thing independently: the round-21 fix named a
    resumed attempt's jsonl and stderr after its sandbox and left the report,
    verdict and rendered archive named after the attempt alone — so
    `_run_log_files`' globs could never find them and the reaper orphaned them.
    hunk-revert could not catch this: it asks whether a test depends on a change,
    not whether a convention is applied everywhere.

    Asserted against the names the READER globs for, so writer and reader cannot
    drift apart without this failing.
    """
    import re
    from pathlib import Path as _Path

    from pilot_workers.cli import dispatch as dispatch_mod
    from pilot_workers.cli import run as run_mod
    from pilot_workers import fmt_events

    # The reader's list, from the source of truth.
    reader = _Path(maintain.__file__).read_text(encoding="utf-8")
    globbed = set(re.findall(r'f"(?:rendered-)?\{run_id\}([^"]*)"', reader))
    assert "+*" in globbed, "the reader no longer globs the resumed-attempt form"

    # Every writer must derive its name from the same stem, never from run_id.
    writers = {
        "run.py (jsonl/stderr/rendered)": _Path(run_mod.__file__),
        "dispatch.py (report/verdict)": _Path(dispatch_mod.__file__),
    }
    offenders = []
    for label, path in writers.items():
        src = path.read_text(encoding="utf-8")
        for match in re.finditer(
                r'f"\{(\w+)\}\.(jsonl|stderr\.log|report\.md|verdict\.json)"', src):
            if match.group(1) not in ("log_stem", "artifact_stem"):
                offenders.append(f"{label}: {match.group(0)}")
        for match in re.finditer(r'f"rendered-\{(\w+)\}\.log"', src):
            # `run_id` is no longer whitelisted here: FmtWriter's parameter was
            # renamed `run_stem` precisely so this pattern needs no exception.
            # kimi noticed the whitelist outlived its reason.
            if match.group(1) not in ("log_stem", "artifact_stem", "run_stem"):
                offenders.append(f"{label}: {match.group(0)}")
    assert not offenders, (
        "a per-run artifact named from something other than the shared stem: "
        + "; ".join(offenders))

    # And the renderer is handed the stem, not the attempt id.
    run_src = _Path(run_mod.__file__).read_text(encoding="utf-8")
    assert "FmtWriter(\n                    logs, provider.key, log_stem" in run_src \
        or "FmtWriter(logs, provider.key, log_stem" in run_src, (
            "FmtWriter is not given the shared log stem")
    assert callable(fmt_events.FmtWriter)
