"""Offline tests for pilot_workers.cli.install (v0.3.0 grammar).

All tests isolate the pilot home via PILOT_WORKERS_HOME and pass --target
pointing at tmp_path, so real ~/.claude and ~/.codex are never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.cli import install as install_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Isolate pilot home and provide a fake install target."""
    home = tmp_path / "home"
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(home))
    target = tmp_path / "target"
    return {"home": home, "target": target}


def _manifest_path() -> object:
    return providers.pilot_home() / "install-manifest.json"


def _read_manifest() -> dict:
    return json.loads(_manifest_path().read_text(encoding="utf-8"))


def _write_v1_manifest(home_entries: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "hosts": home_entries,
    }), encoding="utf-8")


def test_install_provider_on_claude_writes_manifest(isolated):
    rc = install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 0

    manifest_path = _manifest_path()
    assert manifest_path.is_file()
    manifest = _read_manifest()
    assert manifest["schema_version"] == 2
    entry = manifest["installs"]["claude"]["glm"]
    # glm: 4 agents + 4 commands.
    assert len(entry["files"]) == 8
    # Every recorded file path must exist on disk under the target.
    for name in entry["files"]:
        assert Path(name).is_file()
        assert Path(name).is_relative_to(isolated["target"].resolve())
    assert entry["created_dirs"]
    assert entry["installed_at"]
    assert entry["package_version"]


def test_install_single_provider_only_copies_that_providers_files(isolated):
    target = isolated["target"]
    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0

    installed = [p for p in target.rglob("*") if p.is_file()]
    assert installed
    for path in installed:
        assert "ds" not in path.name
        assert "kimi" not in str(path)
    assert len([p for p in target.rglob("*.md")]) == 8


def test_install_provider_without_commands_dir_installs_agents_only(isolated):
    # ds has no claude-host/commands/ds directory.
    target = isolated["target"]
    assert install_mod.main(["ds", "on", "claude", "--target", str(target)]) == 0

    entry = _read_manifest()["installs"]["claude"]["ds"]
    assert len(entry["files"]) == 4
    assert all(Path(name).parent.name == "agents" for name in entry["files"])


def test_install_kimi_uses_asset_prefix(isolated):
    target = isolated["target"]
    assert install_mod.main(["kimi-k3", "on", "claude", "--target", str(target)]) == 0

    entry = _read_manifest()["installs"]["claude"]["kimi-k3"]
    # asset_prefix 'kimi': 4 agents + 4 commands.
    assert len(entry["files"]) == 8
    assert any("kimi-coder.md" in name for name in entry["files"])


def test_reinstall_purges_stale_files(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
    first_files = list(_read_manifest()["installs"]["claude"]["glm"]["files"])

    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
    out = capsys.readouterr().out
    assert "stale file" in out

    manifest = _read_manifest()
    second_files = manifest["installs"]["claude"]["glm"]["files"]
    assert len(second_files) == 8
    assert sorted(second_files) == sorted(first_files)

    # No duplicated files on disk.
    installed = [p for p in Path(target).rglob("*.md") if p.is_file()]
    assert len(installed) == 8


def test_install_provider_on_codex_records_skills(isolated):
    rc = install_mod.main(["glm", "on", "codex", "--target", str(isolated["target"])])
    assert rc == 0

    entry = _read_manifest()["installs"]["codex"]["glm"]
    files = [Path(name) for name in entry["files"]]
    assert any(f.name == "SKILL.md" and "glm" in f.parts for f in files)

    created_dirs = [Path(name) for name in entry["created_dirs"]]
    assert any(d.name == "glm" for d in created_dirs)


def test_install_all_on_all_full_matrix(isolated):
    rc = install_mod.main(["all", "on", "all", "--target", str(isolated["target"])])
    assert rc == 0

    installs = _read_manifest()["installs"]
    assert set(installs) == {"claude", "codex"}
    for host in ("claude", "codex"):
        assert set(installs[host]) == {"ds", "glm", "kimi-k3"}
    assert len(installs["claude"]["ds"]["files"]) == 4
    assert len(installs["claude"]["glm"]["files"]) == 8
    assert len(installs["claude"]["kimi-k3"]["files"]) == 8
    for key in ("ds", "glm", "kimi-k3"):
        assert len(installs["codex"][key]["files"]) == 2


def test_install_provider_on_all_hosts(isolated):
    rc = install_mod.main(["glm", "on", "all", "--target", str(isolated["target"])])
    assert rc == 0
    installs = _read_manifest()["installs"]
    assert set(installs) == {"claude", "codex"}
    assert set(installs["claude"]) == {"glm"}
    assert set(installs["codex"]) == {"glm"}


def test_deprecated_host_alias_maps_to_all_on_host(isolated, capsys):
    rc = install_mod.main(["claude", "--target", str(isolated["target"])])
    assert rc == 0
    err = capsys.readouterr().err
    assert "is deprecated" in err
    assert "install all on claude" in err

    installs = _read_manifest()["installs"]
    assert set(installs) == {"claude"}
    assert set(installs["claude"]) == {"ds", "glm", "kimi-k3"}


def test_deprecated_bare_all_alias_maps_to_all_on_all(isolated, capsys):
    rc = install_mod.main(["all", "--target", str(isolated["target"])])
    assert rc == 0
    assert "is deprecated" in capsys.readouterr().err
    assert set(_read_manifest()["installs"]) == {"claude", "codex"}


def test_grammar_error_missing_on_suggests_fix(isolated, capsys):
    rc = install_mod.main(["glm", "claude"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "did you mean 'install glm on claude'?" in err


def test_grammar_error_unknown_provider(isolated, capsys):
    rc = install_mod.main(["bogus", "on", "claude"])
    assert rc == 2
    assert "unknown provider" in capsys.readouterr().err


def test_grammar_error_empty_argv(isolated, capsys):
    rc = install_mod.main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_grammar_error_unknown_runner(isolated, capsys):
    rc = install_mod.main(["runner", "bogus"])
    assert rc == 2
    assert "unknown runner" in capsys.readouterr().err


def test_v1_manifest_legacy_entry_purged_on_install(isolated, capsys):
    target = isolated["target"]
    stale = target / "agents" / "old-agent.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    _write_v1_manifest({"claude": {
        "installed_at": "2025-01-01T00:00:00+00:00",
        "package_version": "0.2.0",
        "files": [str(stale)],
        "created_dirs": [str(stale.parent)],
    }})

    rc = install_mod.main(["glm", "on", "claude", "--target", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "note: replacing legacy v0.2.0 install on claude" in out
    assert not stale.exists()

    manifest = _read_manifest()
    assert manifest["schema_version"] == 2
    assert "glm" in manifest["installs"]["claude"]
    assert "__all__" not in manifest["installs"]["claude"]


def test_uninstall_all_on_host_clears_legacy_entry(isolated):
    target = isolated["target"]
    stale = target / "agents" / "old-agent.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale", encoding="utf-8")
    _write_v1_manifest({"claude": {
        "installed_at": "2025-01-01T00:00:00+00:00",
        "package_version": "0.2.0",
        "files": [str(stale)],
        "created_dirs": [str(stale.parent)],
    }})

    rc = install_mod.uninstall_main(["all", "on", "claude"])
    assert rc == 0
    assert not stale.exists()
    # claude held only the legacy entry → manifest file is gone.
    assert not _manifest_path().exists()


def test_uninstall_pair_removes_files_and_manifest_entry(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["all", "on", "all", "--target", target]) == 0
    glm_files = list(_read_manifest()["installs"]["claude"]["glm"]["files"])

    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 0

    # Every file recorded for glm on claude is gone from disk.
    for name in glm_files:
        assert not Path(name).exists()

    installs = _read_manifest()["installs"]
    assert "glm" not in installs["claude"]
    # Other providers and hosts are untouched.
    assert "ds" in installs["claude"]
    assert "glm" in installs["codex"]


def test_uninstall_all_on_all_deletes_manifest_file(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["all", "on", "all", "--target", target]) == 0
    assert _manifest_path().is_file()

    assert install_mod.uninstall_main(["all", "on", "all"]) == 0
    assert not _manifest_path().exists()


def test_uninstall_deprecated_alias(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0

    rc = install_mod.uninstall_main(["claude"])
    assert rc == 0
    assert "is deprecated" in capsys.readouterr().err
    assert not _manifest_path().exists()


def test_uninstall_without_manifest_returns_1(isolated, capsys):
    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err


def test_uninstall_pair_missing_from_manifest_returns_1(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0

    rc = install_mod.uninstall_main(["ds", "on", "claude"])
    assert rc == 1
    assert "no manifest entry" in capsys.readouterr().err
    # glm entry is untouched.
    assert "glm" in _read_manifest()["installs"]["claude"]


def test_uninstall_partial_missing_warns_and_continues(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0

    rc = install_mod.uninstall_main(["glm", "on", "all"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no manifest entry for glm on codex" in err
    assert not _manifest_path().exists()


def test_uninstall_runner_missing_is_note_exit_0(isolated, capsys):
    rc = install_mod.uninstall_main(["runner", "opencode"])
    assert rc == 0
    assert "no runner install found" in capsys.readouterr().out


def test_uninstall_runner_removes_tree(isolated, capsys):
    runtime_root = isolated["home"] / "worker-runtime" / "opencode"
    for version in ("0.0.1", "9.9.9"):
        binary_dir = runtime_root / version / "node_modules" / ".bin"
        binary_dir.mkdir(parents=True)
        (binary_dir / "opencode").write_text("#!/bin/sh\n", encoding="utf-8")

    rc = install_mod.uninstall_main(["runner", "opencode"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "removed:" in out
    assert not runtime_root.exists()


def test_install_invalid_host_returns_2(capsys):
    rc = install_mod.main(["glm", "on", "bogus"])
    assert rc == 2
    assert "unknown host" in capsys.readouterr().err.lower() or "usage" in capsys.readouterr().err.lower()


def test_install_target_equals_syntax(isolated):
    target = str(isolated["target"])
    rc = install_mod.main(["glm", "on", "claude", f"--target={target}"])
    assert rc == 0
    manifest = _read_manifest()
    assert "glm" in manifest["installs"]["claude"]


def test_install_runner_unknown_returns_2(capsys):
    rc = install_mod.main(["runner", "nonexistent"])
    assert rc == 2
    assert "unknown runner" in capsys.readouterr().err


def test_install_runner_with_target_returns_2(capsys):
    rc = install_mod.main(["runner", "opencode", "--target", "/tmp/x"])
    assert rc == 2
    assert "--target" in capsys.readouterr().err


def test_uninstall_with_target_returns_2(capsys):
    rc = install_mod.uninstall_main(["glm", "on", "claude", "--target", "/tmp/x"])
    assert rc == 2
    assert "--target" in capsys.readouterr().err


def test_install_runner_opencode_happy_path(monkeypatch, tmp_path):
    import subprocess
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    calls = []
    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr("subprocess.run", mock_run)
    rc = install_mod.main(["runner", "opencode"])
    assert rc == 0
    assert any("install_runtime" in str(c) for c in calls)
