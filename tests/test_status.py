"""Offline tests for pilot_workers.cli.status.

All tests isolate the pilot home via PILOT_WORKERS_HOME and install with
--target pointing at tmp_path, so real ~/.claude and ~/.codex are never
touched.
"""

from __future__ import annotations

import json

import pytest

from pilot_workers.cli import install as install_mod
from pilot_workers.cli import status as status_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolate pilot home and provide a fake install target."""
    home = tmp_path / "home"
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(home))
    target = tmp_path / "target"
    return {"home": home, "target": target}


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
    # Empty environment: no credentials, no installs, no runner binary.
    assert "missing" in out


def test_json_shape(isolated, capsys):
    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert set(data["providers"]) == {"ds", "glm", "kimi-k3"}
    glm = data["providers"]["glm"]
    assert glm["credential"]["configured"] is False
    assert str(isolated["home"].resolve()) in glm["credential"]["path"]
    assert glm["hosts"] == {"claude": "-", "codex": "-"}

    opencode = data["runners"]["opencode"]
    assert opencode["present"] is False
    assert opencode["version"] is None
    assert opencode["pinned"]
    assert str(isolated["home"].resolve()) in opencode["binary"]


def test_json_reflects_install(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["providers"]["glm"]["hosts"]["claude"] == "installed"
    assert data["providers"]["glm"]["hosts"]["codex"] == "-"
    assert data["providers"]["ds"]["hosts"]["claude"] == "-"


def test_pair_status_not_installed(isolated, capsys):
    assert status_mod.main(["glm", "on", "claude"]) == 0
    assert "not installed" in capsys.readouterr().out


def test_pair_status_installed_lists_files(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
    capsys.readouterr()

    assert status_mod.main(["glm", "on", "claude"]) == 0
    out = capsys.readouterr().out
    assert "glm on claude: installed" in out
    assert "glm-coder.md" in out


def test_pair_status_json(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
    capsys.readouterr()

    assert status_mod.main(["glm", "on", "claude", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "glm"
    assert payload["host"] == "claude"
    assert payload["installed"] is True
    assert payload["legacy"] is False
    assert len(payload["entry"]["files"]) == 8


def test_pair_status_unknown_provider_returns_2(isolated, capsys):
    assert status_mod.main(["bogus", "on", "claude"]) == 2
    assert "unknown provider" in capsys.readouterr().err


def test_status_bad_grammar_returns_2(isolated, capsys):
    assert status_mod.main(["glm", "claude"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_status_reports_legacy_v1_install(isolated, capsys):
    from pilot_workers import providers
    target = str(isolated["target"])
    manifest_path = providers.pilot_home() / "install-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    v1 = {"schema_version": 1, "hosts": {"claude": {
        "installed_at": "2026-01-01", "package_version": "0.2.0",
        "files": [f"{target}/agents/glm-coder.md"], "created_dirs": []}}}
    manifest_path.write_text(json.dumps(v1))
    rc = status_mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "legacy" in out.lower()
