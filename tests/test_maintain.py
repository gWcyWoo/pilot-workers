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

import json
import os
import subprocess
import sys
import time

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
    group = next(g for g in groups if any(p.name == "r1.jsonl" for p in g))
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
    assert "skipping (live)" in capsys.readouterr().out


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
