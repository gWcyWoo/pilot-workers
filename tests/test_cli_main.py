"""Offline tests for the unified entry point pilot_workers.cli.main."""

from __future__ import annotations

import json

import pytest

from pilot_workers.cli import main as main_mod


def test_no_args_prints_usage(capsys):
    assert main_mod.main([]) == 0
    assert "usage: pilot-workers" in capsys.readouterr().out


def test_help_flag_prints_usage(capsys):
    assert main_mod.main(["--help"]) == 0
    assert "usage: pilot-workers" in capsys.readouterr().out


def test_unknown_subcommand_returns_2(capsys):
    assert main_mod.main(["bogus"]) == 2
    assert "unknown subcommand" in capsys.readouterr().err


def test_runtime_without_args_returns_2(capsys):
    assert main_mod.main(["runtime"]) == 2
    assert "runtime install" in capsys.readouterr().err


def test_template_valid_mode_prints_template(capsys):
    assert main_mod.main(["template", "code"]) == 0
    out = capsys.readouterr().out
    assert "# Objective" in out
    assert "Never include any credentials" in out


def test_template_each_mode_nonempty(capsys):
    for mode in ("code", "explore", "test", "review"):
        assert main_mod.main(["template", mode]) == 0
        assert len(capsys.readouterr().out) > 100


def test_template_invalid_mode_returns_2(capsys):
    assert main_mod.main(["template", "resume"]) == 2
    assert "usage: pilot-workers template" in capsys.readouterr().err


def test_template_missing_arg_returns_2(capsys):
    assert main_mod.main(["template"]) == 2


def test_runtime_with_unknown_action_returns_2(capsys):
    assert main_mod.main(["runtime", "bogus"]) == 2
    assert "runtime install" in capsys.readouterr().err


def test_run_dry_run_emits_json(tmp_path, monkeypatch, capsys):
    # Isolate pilot home so credential/profile lookups stay in tmp_path.
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    rc = main_mod.main([
        "run",
        "--provider", "glm",
        "--mode", "explore",
        "--workdir", ".",
        "--task", "h",
        "--dry-run",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "worker_runner.dry_run"


def test_install_help_raises_system_exit_0():
    with pytest.raises(SystemExit) as excinfo:
        main_mod.main(["install", "--help"])
    assert excinfo.value.code == 0


def test_uninstall_routes_and_fails_without_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    rc = main_mod.main(["uninstall", "claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err
