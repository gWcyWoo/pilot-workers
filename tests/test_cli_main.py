"""Offline tests for the unified entry point pilot_workers.cli.main."""

from __future__ import annotations

import json

from pilot_workers.cli import main as main_mod


def test_no_args_prints_usage(capsys):
    assert main_mod.main([]) == 0
    assert "pw9" in capsys.readouterr().out or "pilot-workers" in capsys.readouterr().out


def test_help_flag_prints_usage(capsys):
    assert main_mod.main(["--help"]) == 0


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


def test_template_each_mode_nonempty(capsys):
    for mode in ("code", "explore", "test", "review"):
        assert main_mod.main(["template", mode]) == 0
        assert len(capsys.readouterr().out) > 100


def test_template_invalid_mode_returns_2(capsys):
    assert main_mod.main(["template", "resume"]) == 2


def test_run_dry_run_emits_json(tmp_path, monkeypatch, capsys):
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


def test_status_route_prints_table_headers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    rc = main_mod.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROVIDER" in out
    assert "RUNNER" in out


def test_install_help(capsys):
    assert main_mod.main(["install", "--help"]) == 0
    out = capsys.readouterr().out
    assert "runner" in out


def test_usage_lists_subcommands(capsys):
    main_mod.main([])
    out = capsys.readouterr().out
    for cmd in ("status", "review", "test", "permissions", "key"):
        assert cmd in out, f"usage missing {cmd}"


def test_key_subcommand_routes(tmp_path, monkeypatch, capsys):
    assert main_mod.main(["key", "--help"]) == 0


def test_review_subcommand_routes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    assert main_mod.main(["review", "show"]) == 0
    assert "axes" in capsys.readouterr().out.lower()


def test_test_subcommand_routes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    assert main_mod.main(["test", "show"]) == 0
    assert "layers" in capsys.readouterr().out.lower()
