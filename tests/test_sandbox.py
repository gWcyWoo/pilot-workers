"""RED tests for D5 — per-run sandbox (design v0.5.0).

Covers the pinned v2 API surface:
- providers.runs_root / providers.run_paths
- runtime.process_start_time / read_run_lock / lock_is_stale /
  acquire_run_lock / release_run_lock / provision_run_sandbox /
  build_environment(..., paths=...)
- OpenCodeRunner.runner_environment(..., paths=...)
- cli/run.py --run-id parse + validate plumbing for resume mode

All tests are offline and tmp_path-isolated via PILOT_WORKERS_HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys

import pytest

from pilot_workers import providers, runtime
from pilot_workers.cli import run as run_mod
from pilot_workers.runners import get_runner


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def provider():
    return providers.PROVIDERS["glm"]


def _dead_pid() -> int:
    """Pid of a real short-lived child that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# providers.runs_root / providers.run_paths
# ---------------------------------------------------------------------------


def test_runs_root_is_profile_root_runs(home, provider):
    assert providers.runs_root(provider) == providers.profile_root(provider) / "runs"


def test_run_paths_shape(home, provider):
    paths = providers.run_paths(provider, "run-1")
    root = providers.runs_root(provider) / "run-1"
    assert set(paths) == {"root", "config", "data", "state", "cache", "lock"}
    assert paths["root"] == root
    assert paths["config"] == root / "config"
    assert paths["data"] == root / "data"
    assert paths["state"] == root / "state"
    assert paths["cache"] == root / "cache"
    assert paths["lock"] == root / ".lock"


# ---------------------------------------------------------------------------
# runtime.process_start_time
# ---------------------------------------------------------------------------


def test_process_start_time_current_process_available():
    token = runtime.process_start_time(os.getpid())
    assert token is not None
    assert isinstance(token, str)
    assert token.strip()


def test_process_start_time_stable_for_same_pid():
    first = runtime.process_start_time(os.getpid())
    second = runtime.process_start_time(os.getpid())
    assert first is not None
    assert first == second


# ---------------------------------------------------------------------------
# runtime.read_run_lock / lock_is_stale
# ---------------------------------------------------------------------------


def test_read_run_lock_missing_returns_none(tmp_path):
    assert runtime.read_run_lock(tmp_path) is None


def test_read_run_lock_parses_json(tmp_path):
    payload = {"pid": 1234, "started_at": "some-token"}
    (tmp_path / ".lock").write_text(json.dumps(payload), encoding="utf-8")
    assert runtime.read_run_lock(tmp_path) == payload


def test_lock_is_stale_live_pid_matching_start_time():
    lock = {"pid": os.getpid(), "started_at": runtime.process_start_time(os.getpid())}
    assert runtime.lock_is_stale(lock) is False


def test_lock_is_stale_live_pid_mismatched_start_time():
    lock = {"pid": os.getpid(), "started_at": "bogus"}
    assert runtime.lock_is_stale(lock) is True


def test_lock_is_stale_dead_pid():
    lock = {"pid": _dead_pid(), "started_at": "anything"}
    assert runtime.lock_is_stale(lock) is True


# ---------------------------------------------------------------------------
# runtime.acquire_run_lock / release_run_lock
# ---------------------------------------------------------------------------


def test_acquire_run_lock_creates_lock_with_current_pid(tmp_path):
    runtime.acquire_run_lock(tmp_path)
    try:
        lock = runtime.read_run_lock(tmp_path)
        assert lock is not None
        assert lock["pid"] == os.getpid()
        assert lock["started_at"] == runtime.process_start_time(os.getpid())
    finally:
        runtime.release_run_lock(tmp_path)


def test_acquire_run_lock_live_lock_raises(tmp_path):
    runtime.acquire_run_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="run is still active"):
            runtime.acquire_run_lock(tmp_path)
    finally:
        runtime.release_run_lock(tmp_path)


def test_acquire_run_lock_replaces_stale_lock(tmp_path):
    stale = {"pid": _dead_pid(), "started_at": "whatever"}
    (tmp_path / ".lock").write_text(json.dumps(stale), encoding="utf-8")
    runtime.acquire_run_lock(tmp_path)
    try:
        lock = runtime.read_run_lock(tmp_path)
        assert lock["pid"] == os.getpid()
    finally:
        runtime.release_run_lock(tmp_path)


def test_release_run_lock_removes_lockfile(tmp_path):
    runtime.acquire_run_lock(tmp_path)
    assert (tmp_path / ".lock").exists()
    runtime.release_run_lock(tmp_path)
    assert not (tmp_path / ".lock").exists()


def test_release_run_lock_missing_is_not_an_error(tmp_path):
    runtime.release_run_lock(tmp_path)
    runtime.release_run_lock(tmp_path)


# ---------------------------------------------------------------------------
# runtime.provision_run_sandbox
# ---------------------------------------------------------------------------


def _make_canonical_credential(provider):
    runner = get_runner("opencode")
    canonical = runner.credential_path(provider)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps({"x": {"type": "api", "key": "k"}}), encoding="utf-8")
    return canonical


def test_provision_run_sandbox_layout(home, provider):
    runner = get_runner("opencode")
    canonical = _make_canonical_credential(provider)
    shared_cache = providers.profile_paths(provider)["cache"]

    paths = runtime.provision_run_sandbox(provider, "run-1", runner)
    try:
        assert set(paths) == {"root", "config", "data", "state", "cache", "lock"}
        # per-run dirs exist and are private
        for key in ("root", "config", "data", "state"):
            assert paths[key].is_dir(), key
            assert stat.S_IMODE(paths[key].stat().st_mode) == 0o700, key
        # auth.json zero-copy symlink to the canonical credential
        auth_link = paths["data"] / "opencode" / "auth.json"
        assert auth_link.is_symlink()
        assert auth_link.resolve() == canonical.resolve()
        # shared per-provider cache dir created and symlinked
        assert shared_cache.is_dir()
        assert paths["cache"].is_symlink()
        assert paths["cache"].resolve() == shared_cache.resolve()
        # run lock acquired with the current pid
        lock = runtime.read_run_lock(paths["root"])
        assert lock is not None
        assert lock["pid"] == os.getpid()
    finally:
        runtime.release_run_lock(paths["root"])


def test_provision_run_sandbox_returns_run_paths(home, provider):
    runner = get_runner("opencode")
    paths = runtime.provision_run_sandbox(provider, "run-2", runner)
    try:
        assert paths == providers.run_paths(provider, "run-2")
    finally:
        runtime.release_run_lock(paths["root"])


def test_provision_run_sandbox_live_lock_raises(home, provider):
    runner = get_runner("opencode")
    paths = runtime.provision_run_sandbox(provider, "run-3", runner)
    try:
        with pytest.raises(RuntimeError, match="run is still active"):
            runtime.provision_run_sandbox(provider, "run-3", runner)
    finally:
        runtime.release_run_lock(paths["root"])


# ---------------------------------------------------------------------------
# runtime.build_environment(..., paths=...)
# ---------------------------------------------------------------------------


def test_build_environment_with_run_paths_points_xdg_at_sandbox(home, provider):
    paths = providers.run_paths(provider, "run-env")
    env = runtime.build_environment(provider, {}, paths=paths)
    assert env["XDG_CONFIG_HOME"] == str(paths["config"])
    assert env["XDG_DATA_HOME"] == str(paths["data"])
    assert env["XDG_STATE_HOME"] == str(paths["state"])
    assert env["XDG_CACHE_HOME"] == str(paths["cache"])


def test_build_environment_default_paths_backward_compatible(home, provider):
    env = runtime.build_environment(provider, {})
    profile = providers.profile_paths(provider)
    assert env["XDG_CONFIG_HOME"] == str(profile["config"])
    assert env["XDG_DATA_HOME"] == str(profile["data"])
    assert env["XDG_STATE_HOME"] == str(profile["state"])
    assert env["XDG_CACHE_HOME"] == str(profile["cache"])


# ---------------------------------------------------------------------------
# OpenCodeRunner.runner_environment(..., paths=...)
# ---------------------------------------------------------------------------


def test_runner_environment_with_run_paths(home, provider):
    runner = get_runner("opencode")
    config = runner.build_config(provider, "code")
    paths = providers.run_paths(provider, "run-cfg")
    env = runner.runner_environment(provider, config, paths=paths)
    assert env["OPENCODE_CONFIG_DIR"] == str(paths["config"] / "opencode")


def test_runner_environment_default_paths_backward_compatible(home, provider):
    runner = get_runner("opencode")
    config = runner.build_config(provider, "code")
    env = runner.runner_environment(provider, config)
    expected = providers.profile_paths(provider)["config"] / "opencode"
    assert env["OPENCODE_CONFIG_DIR"] == str(expected)


# ---------------------------------------------------------------------------
# cli/run.py --run-id plumbing (resume keyed by --session + --run-id)
# ---------------------------------------------------------------------------


_RUN_BASE_ARGV = ["--provider", "glm", "--workdir", "/tmp", "--task", "do it"]


def _mode_ns(**overrides):
    base = {"mode": "code", "session": None, "worktree": False, "run_id": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_parse_args_accepts_run_id():
    args = run_mod.parse_args(
        _RUN_BASE_ARGV
        + ["--mode", "resume", "--session", "s-1", "--run-id", "r-1"]
    )
    assert args.run_id == "r-1"


def test_run_parse_args_run_id_defaults_to_none():
    args = run_mod.parse_args(_RUN_BASE_ARGV + ["--mode", "code"])
    assert args.run_id is None


def test_run_validate_resume_requires_run_id():
    with pytest.raises(RuntimeError, match="--run-id"):
        run_mod.validate_mode_arguments(_mode_ns(mode="resume", session="s-1"))


def test_run_validate_resume_requires_session():
    with pytest.raises(RuntimeError, match="--session"):
        run_mod.validate_mode_arguments(_mode_ns(mode="resume", run_id="r-1"))


def test_run_validate_resume_with_session_and_run_id_ok():
    run_mod.validate_mode_arguments(
        _mode_ns(mode="resume", session="s-1", run_id="r-1"))


def test_run_validate_run_id_outside_resume_raises():
    with pytest.raises(RuntimeError, match="--run-id"):
        run_mod.validate_mode_arguments(_mode_ns(mode="code", run_id="r-1"))
