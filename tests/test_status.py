"""Offline tests for pilot_workers.cli.status (simplified: credentials + runners).

All tests isolate the pilot home via PILOT_WORKERS_HOME.
"""

from __future__ import annotations

import json

import pytest

from pilot_workers.cli import status as status_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_providers_table_lists_all_providers(isolated, capsys):
    assert status_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "Providers" in out
    assert "PROVIDER" in out
    assert "CREDENTIAL" in out
    assert "Runners" in out
    assert "RUNNER" in out
    for key in ("glm", "kimi-k3", "ds"):
        assert key in out
    assert "opencode" in out
    assert "missing" in out


def test_json_shape(isolated, capsys):
    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data["providers"]) == {"claude", "codex", "ds", "glm", "kimi-k3"}
    glm = data["providers"]["glm"]
    assert glm["credential"]["configured"] is False
    assert str(isolated.resolve()) in glm["credential"]["path"]

    opencode = data["runners"]["opencode"]
    assert opencode["present"] is False
    assert opencode["version"] is None
    assert opencode["pinned"]
    assert str(isolated.resolve()) in opencode["binary"]

    # No installs section in the new simplified status.
    assert "installs" not in data


def test_status_with_args_returns_2(isolated, capsys):
    assert status_mod.main(["bogus"]) == 2


def test_help_returns_0(isolated, capsys):
    assert status_mod.main(["--help"]) == 0
