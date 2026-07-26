"""Offline tests for pilot_workers.cli.status (v0.5.0 host-level installs).

All tests isolate the pilot home via PILOT_WORKERS_HOME, so real ~/.claude
and ~/.codex are never touched.

v3 contract (design-v0.5.0 D2): the overview keeps per-provider credential
status, but installs are reported per HOST (present/absent + file count)
alongside runner presence. `status <host>` is the detail form; the old
`status <provider> on <host>` pair form is a usage error (exit 2).
`status --json` keys installs by host only.
"""

from __future__ import annotations

import json

import pytest

from pilot_workers import providers
from pilot_workers.cli import status as status_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolate pilot home and provide a fake install target."""
    home = tmp_path / "home"
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(home))
    target = tmp_path / "target"
    return {"home": home, "target": target}


def _write_v3_manifest(installs: dict) -> None:
    path = providers.pilot_home() / "install-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 4,
        "installs": installs,
    }), encoding="utf-8")


def _v3_entry(files: list[str]) -> dict:
    return {
        "installed_at": "2026-07-24T00:00:00+00:00",
        "package_version": "0.5.0",
        "files": files,
        "created_dirs": [],
    }


# ----------------------------------------------------------------------
# overview: credentials per provider (unchanged)
# ----------------------------------------------------------------------


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

    assert set(data["providers"]) == {"ds", "glm", "kimi-k3", "test-case"}
    glm = data["providers"]["glm"]
    assert glm["credential"]["configured"] is False
    assert str(isolated["home"].resolve()) in glm["credential"]["path"]

    opencode = data["runners"]["opencode"]
    assert opencode["present"] is False
    assert opencode["version"] is None
    assert opencode["pinned"]
    assert str(isolated["home"].resolve()) in opencode["binary"]

    # Installs are reported host-level.
    assert "installs" in data
    for host in data["installs"]:
        assert host in providers.HOSTS  # not a hardcoded pair


# ----------------------------------------------------------------------
# overview: installs per host
# ----------------------------------------------------------------------


def test_overview_lists_host_installs(isolated, capsys):
    _write_v3_manifest({"claude": _v3_entry([
        "/tmp/x/skills/pilot-workers/SKILL.md",
        "/tmp/x/skills/pilot-workers/references/dispatch.md",
    ])})

    assert status_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "Installs" in out
    # Host row shows presence plus file count.
    claude_lines = [
        line for line in out.splitlines() if "claude" in line.lower()]
    assert claude_lines
    assert any("2" in line for line in claude_lines)


def test_json_installs_keyed_by_host_only(isolated, capsys):
    _write_v3_manifest({"claude": _v3_entry([
        "/tmp/x/skills/pilot-workers/SKILL.md",
        "/tmp/x/skills/pilot-workers/references/dispatch.md",
    ])})

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "claude" in data["installs"]
    for host, info in data["installs"].items():
        assert host in providers.HOSTS  # not a hardcoded pair
        # No provider nesting under a host entry.
        for key in ("glm", "kimi-k3", "ds", "__all__"):
            assert key not in info


# ----------------------------------------------------------------------
# detail form: status <host>
# ----------------------------------------------------------------------


def test_status_host_detail_installed(isolated, capsys):
    _write_v3_manifest({"claude": _v3_entry([
        "/tmp/x/skills/pilot-workers/SKILL.md",
        "/tmp/x/skills/pilot-workers/references/dispatch.md",
    ])})

    assert status_mod.main(["claude"]) == 0
    out = capsys.readouterr().out
    assert "claude" in out
    assert "installed" in out.lower()


def test_status_host_detail_not_installed(isolated, capsys):
    assert status_mod.main(["codex"]) == 0
    out = capsys.readouterr().out
    assert "not installed" in out.lower()


def test_status_unknown_host_returns_2(isolated, capsys):
    assert status_mod.main(["bogus"]) == 2
    assert "usage:" in capsys.readouterr().err


# ----------------------------------------------------------------------
# removed pair form
# ----------------------------------------------------------------------


def test_status_provider_on_host_pair_is_usage_error(isolated, capsys):
    assert status_mod.main(["glm", "on", "claude"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_status_bad_grammar_returns_2(isolated, capsys):
    assert status_mod.main(["glm", "claude"]) == 2
    assert "usage:" in capsys.readouterr().err
