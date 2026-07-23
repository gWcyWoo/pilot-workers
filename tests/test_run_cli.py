"""Offline tests for pilot_workers.cli.run helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.cli import run as run_mod


def _args(**overrides):
    base = {
        "task": None,
        "task_file": None,
        "mode": "code",
        "session": None,
        "worktree": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_load_task_blank_task_raises():
    with pytest.raises(RuntimeError, match="must not be empty"):
        run_mod.load_task(_args(task="   "))


def test_load_task_oversized_task_raises():
    big = "x" * (providers.MAX_TASK_BYTES + 1)
    with pytest.raises(RuntimeError, match="exceeds"):
        run_mod.load_task(_args(task=big))


def test_load_task_missing_file_raises(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(RuntimeError, match="does not exist"):
        run_mod.load_task(_args(task_file=str(missing)))


def test_load_task_returns_stripped_text():
    assert run_mod.load_task(_args(task="  hello task  \n")) == "hello task"


def test_load_task_from_file(tmp_path):
    path = tmp_path / "task.txt"
    path.write_text("  file task \n", encoding="utf-8")
    assert run_mod.load_task(_args(task_file=str(path))) == "file task"


def test_validate_resume_without_session_raises():
    with pytest.raises(RuntimeError, match="--session is required"):
        run_mod.validate_mode_arguments(_args(mode="resume"))


def test_validate_non_resume_with_session_raises():
    with pytest.raises(RuntimeError, match="only valid with --mode resume"):
        run_mod.validate_mode_arguments(_args(mode="code", session="s-1"))


def test_validate_resume_with_worktree_raises():
    with pytest.raises(RuntimeError, match="worktree"):
        run_mod.validate_mode_arguments(
            _args(mode="resume", session="s-1", worktree=True))


def test_validate_resume_with_session_ok():
    run_mod.validate_mode_arguments(_args(mode="resume", session="s-1"))


def test_dry_run_summary_default_permission_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    summary = run_mod.dry_run_summary(
        providers.PROVIDERS["glm"], "explore", Path("."))
    assert summary["type"] == "worker_runner.dry_run"
    assert summary["permission_profile"] is None


def test_dry_run_summary_explicit_permission_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    summary = run_mod.dry_run_summary(
        providers.PROVIDERS["glm"], "explore", Path("."),
        permission_profile="relaxed")
    assert summary["permission_profile"] == "relaxed"
