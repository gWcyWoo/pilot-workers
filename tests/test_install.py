"""Offline tests for pilot_workers.cli.install (v0.5.0 host-level grammar).

All tests isolate the pilot home via PILOT_WORKERS_HOME and pass --target
pointing at tmp_path, so real ~/.claude and ~/.codex are never touched.

v3 contract (design-v0.5.0 D2):
- Grammar: install|uninstall <host|all>, install|uninstall runner <name>.
  The provider dimension and the `on` keyword are gone; removed forms yield
  usage errors (exit 2, message contains "usage:").
- Assets: INTEGRATIONS_DIR/<host>-host/skills/pilot-workers/ is installed
  host-level by install_host(host, target) -> {"files", "created_dirs"}.
- Manifest v3: {"schema_version": 3, "installs": {"<host>": {...}}} with no
  provider nesting; installing over a v1/v2 manifest purges every legacy
  entry for that host (one printed "removed" line per removed file) and the
  on-disk file is rewritten as v3.
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


def _manifest_path() -> Path:
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


def _write_v2_manifest(installs: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 2,
        "installs": installs,
    }), encoding="utf-8")


def _make_legacy_files(base: Path, rel_names: list[str]) -> list[Path]:
    paths = []
    for rel in rel_names:
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("legacy", encoding="utf-8")
        paths.append(p)
    return paths


def _legacy_entry(files: list[Path], version: str = "0.4.0") -> dict:
    return {
        "installed_at": "2025-01-01T00:00:00+00:00",
        "package_version": version,
        "files": [str(p) for p in files],
        "created_dirs": [],
    }


# ----------------------------------------------------------------------
# grammar: accepted forms
# ----------------------------------------------------------------------


def test_parse_install_claude_accepted(capsys):
    spec = install_mod._parse_grammar(["claude"], "install", install_mod.INSTALL_USAGE)
    assert spec["host"] == "claude"
    # The provider dimension is gone from the grammar.
    assert "provider" not in spec
    # 'install claude' is the canonical form: no deprecation note.
    assert "deprecated" not in capsys.readouterr().err


def test_parse_uninstall_codex_accepted(capsys):
    spec = install_mod._parse_grammar(
        ["codex"], "uninstall", install_mod.UNINSTALL_USAGE)
    assert spec["host"] == "codex"
    assert "provider" not in spec
    assert "deprecated" not in capsys.readouterr().err


def test_parse_install_all_accepted(capsys):
    spec = install_mod._parse_grammar(["all"], "install", install_mod.INSTALL_USAGE)
    assert spec["host"] == "all"
    assert "provider" not in spec
    assert "deprecated" not in capsys.readouterr().err


def test_runner_grammar_unchanged_parse_only():
    spec = install_mod._parse_grammar(
        ["runner", "opencode"], "install", install_mod.INSTALL_USAGE)
    assert spec["kind"] == "runner"
    assert spec["name"] == "opencode"


# ----------------------------------------------------------------------
# grammar: removed forms are usage errors
# ----------------------------------------------------------------------


def test_install_provider_on_host_is_usage_error(isolated, capsys):
    rc = install_mod.main(
        ["glm", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_uninstall_provider_on_host_is_usage_error(isolated, capsys):
    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_install_skill_on_host_is_usage_error(isolated, capsys):
    rc = install_mod.main(
        ["skill", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_install_unknown_host_is_usage_error(isolated, capsys):
    rc = install_mod.main(["bogus", "--target", str(isolated["target"])])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_grammar_error_empty_argv(isolated, capsys):
    rc = install_mod.main([])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_grammar_error_unknown_runner(isolated, capsys):
    rc = install_mod.main(["runner", "bogus"])
    assert rc == 2
    assert "unknown runner" in capsys.readouterr().err


# ----------------------------------------------------------------------
# install_host asset installer
# ----------------------------------------------------------------------


def test_install_host_claude_copies_skill_tree(isolated):
    target = isolated["target"]
    result = install_mod.install_host("claude", target)

    assert set(result) >= {"files", "created_dirs"}
    assert result["files"]
    skill_root = (target / "skills" / "pilot-workers").resolve()
    for name in result["files"]:
        path = Path(name)
        assert path.is_file()
        assert path.is_relative_to(skill_root)


def test_install_host_codex_copies_skill_tree(isolated, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(isolated["home"] / "codex-home"))
    target = isolated["target"]
    result = install_mod.install_host("codex", target)

    assert result["files"]
    skill_root = (target / "pilot-workers").resolve()
    for name in result["files"]:
        path = Path(name)
        assert path.is_file()
        assert path.is_relative_to(skill_root)


# ----------------------------------------------------------------------
# manifest v3
# ----------------------------------------------------------------------


def test_install_claude_writes_v3_manifest(isolated):
    rc = install_mod.main(["claude", "--target", str(isolated["target"])])
    assert rc == 0

    manifest = _read_manifest()
    assert manifest["schema_version"] == 3
    assert "hosts" not in manifest
    entry = manifest["installs"]["claude"]
    # Host-level entry: flat shape, no provider nesting.
    assert entry["installed_at"]
    assert entry["package_version"]
    assert entry["files"]
    for key in ("glm", "kimi-k3", "ds", "__all__"):
        assert key not in entry
    for name in entry["files"]:
        assert Path(name).is_file()


def test_install_all_installs_both_hosts_v3(isolated):
    rc = install_mod.main(["all", "--target", str(isolated["target"])])
    assert rc == 0

    manifest = _read_manifest()
    assert manifest["schema_version"] == 3
    installs = manifest["installs"]
    assert set(installs) == {"claude", "codex"}
    for host in ("claude", "codex"):
        entry = installs[host]
        assert entry["files"]
        for key in ("glm", "kimi-k3", "ds", "__all__"):
            assert key not in entry


def test_reinstall_purges_previous_host_install(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["claude", "--target", target]) == 0
    first_files = list(_read_manifest()["installs"]["claude"]["files"])
    assert first_files

    assert install_mod.main(["claude", "--target", target]) == 0
    out = capsys.readouterr().out
    # One printed line per removed file.
    removed_lines = [line for line in out.splitlines() if "removed" in line]
    assert len(removed_lines) >= len(first_files)

    second_files = _read_manifest()["installs"]["claude"]["files"]
    assert sorted(second_files) == sorted(first_files)
    # No duplicated files on disk.
    on_disk = [p for p in isolated["target"].rglob("*") if p.is_file()]
    assert len(on_disk) == len(first_files)


# ----------------------------------------------------------------------
# legacy manifest migration on install
# ----------------------------------------------------------------------


def test_v2_manifest_migrated_to_v3_on_install(isolated, capsys):
    target = isolated["target"]
    glm_files = _make_legacy_files(
        target, ["agents/glm-coder.md", "commands/glm/review.md"])
    all_files = _make_legacy_files(target, ["agents/old-bundle.md"])
    legacy = glm_files + all_files
    _write_v2_manifest({"claude": {
        "glm": _legacy_entry(glm_files),
        "__all__": _legacy_entry(all_files, version="0.2.0"),
    }})

    rc = install_mod.main(["claude", "--target", str(target)])
    assert rc == 0
    out = capsys.readouterr().out

    # Every legacy file for the host is purged, one printed line each.
    for p in legacy:
        assert not p.exists()
    removed_lines = [line for line in out.splitlines() if "removed" in line]
    assert len(removed_lines) >= len(legacy)

    # The on-disk manifest is v3: no "hosts" key, no provider nesting.
    manifest = _read_manifest()
    assert manifest["schema_version"] == 3
    assert "hosts" not in manifest
    entry = manifest["installs"]["claude"]
    assert "glm" not in entry
    assert "__all__" not in entry
    assert entry["files"]


def test_v1_manifest_migrated_to_v3_on_install(isolated, capsys):
    target = isolated["target"]
    legacy = _make_legacy_files(target, ["agents/old-agent.md"])
    _write_v1_manifest({"claude": _legacy_entry(legacy, version="0.2.0")})

    rc = install_mod.main(["claude", "--target", str(target)])
    assert rc == 0
    out = capsys.readouterr().out

    for p in legacy:
        assert not p.exists()
    removed_lines = [line for line in out.splitlines() if "removed" in line]
    assert len(removed_lines) >= len(legacy)

    manifest = _read_manifest()
    assert manifest["schema_version"] == 3
    assert "hosts" not in manifest
    entry = manifest["installs"]["claude"]
    assert "__all__" not in entry
    assert entry["files"]


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def test_uninstall_claude_v3_removes_files_and_deletes_manifest(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["claude", "--target", target]) == 0
    files = [Path(n) for n in _read_manifest()["installs"]["claude"]["files"]]
    assert files

    rc = install_mod.uninstall_main(["claude"])
    assert rc == 0

    for p in files:
        assert not p.exists()
    # Empty manifest → file deleted.
    assert not _manifest_path().exists()


def test_uninstall_claude_purges_legacy_v2_entries(isolated, capsys):
    target = isolated["target"]
    glm_files = _make_legacy_files(target, ["agents/glm-coder.md"])
    all_files = _make_legacy_files(target, ["agents/old-bundle.md"])
    _write_v2_manifest({"claude": {
        "glm": _legacy_entry(glm_files),
        "__all__": _legacy_entry(all_files, version="0.2.0"),
    }})

    rc = install_mod.uninstall_main(["claude"])
    assert rc == 0
    # No deprecation note on the canonical host form.
    assert "deprecated" not in capsys.readouterr().err

    for p in glm_files + all_files:
        assert not p.exists()
    assert not _manifest_path().exists()


def test_uninstall_claude_purges_legacy_v1_entry(isolated):
    target = isolated["target"]
    legacy = _make_legacy_files(target, ["agents/old-agent.md"])
    _write_v1_manifest({"claude": _legacy_entry(legacy, version="0.2.0")})

    rc = install_mod.uninstall_main(["claude"])
    assert rc == 0
    for p in legacy:
        assert not p.exists()
    assert not _manifest_path().exists()


def test_uninstall_without_manifest_returns_1(isolated, capsys):
    rc = install_mod.uninstall_main(["claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err


def test_uninstall_with_target_returns_2(capsys):
    rc = install_mod.uninstall_main(["claude", "--target", "/tmp/x"])
    assert rc == 2
    assert "--target" in capsys.readouterr().err


# ----------------------------------------------------------------------
# runner branch (unchanged)
# ----------------------------------------------------------------------


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


def test_install_runner_unknown_returns_2(capsys):
    rc = install_mod.main(["runner", "nonexistent"])
    assert rc == 2
    assert "unknown runner" in capsys.readouterr().err


def test_install_runner_with_target_returns_2(capsys):
    rc = install_mod.main(["runner", "opencode", "--target", "/tmp/x"])
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
