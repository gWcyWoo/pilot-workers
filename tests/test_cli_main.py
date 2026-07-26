"""Offline tests for the unified entry point pilot_workers.cli.main."""

from __future__ import annotations

import json

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
    # The credential warning used to live here as prose. It is now a hard check
    # in taskguard, so assert the CHECK exists rather than the wording.
    from pilot_workers import taskguard
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task("token: ghp_" + "a" * 24, known_secrets=[])


def test_template_each_mode_nonempty(capsys):
    for mode in ("code", "explore", "test", "test-case", "review"):
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


def test_install_help_prints_new_grammar(capsys):
    assert main_mod.main(["install", "--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: pilot-workers install" in out
    assert "on <host|all>" in out
    assert "runner <name>" in out


def test_usage_mentions_new_grammar(capsys):
    assert main_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "install <host|all>" in out
    assert "install <provider|all> on <host|all>" not in out
    assert "status" in out


def test_status_route_prints_table_headers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    rc = main_mod.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PROVIDER" in out
    assert "RUNNER" in out


def test_uninstall_routes_and_fails_without_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    rc = main_mod.main(["uninstall", "claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_usage_does_not_deny_the_implemented_grammar(capsys):
    """The top-level usage claimed `<provider> on <host>` was 'no longer
    accepted' — the first screen a user sees, contradicting what the parser now
    does."""
    main_mod.main([])
    out = _flat(capsys.readouterr().out)
    assert "no longer accepted" not in out


def test_usage_lists_the_provider_forms(capsys):
    main_mod.main([])
    out = _flat(capsys.readouterr().out)
    assert "<provider> on <host>" in out
    assert "for <mode>" in out


def test_usage_matches_the_install_parser(capsys):
    """Every form the usage advertises must actually parse."""
    from pilot_workers.cli import install as install_mod

    for argv in (["claude"], ["all"], ["runner", "opencode"],
                 ["glm", "on", "claude"], ["glm", "on", "claude", "for", "code"]):
        spec = install_mod._parse_grammar(
            argv, "install", install_mod.INSTALL_USAGE)
        assert spec, f"advertised form does not parse: {argv}"
