"""Offline tests for pilot_workers.cli.install.

All tests isolate the pilot home via PILOT_WORKERS_HOME and pass --target
pointing at tmp_path, so real ~/.claude and ~/.codex are never touched.

v4 contract:
- Grammar: install|uninstall <host|all>, install|uninstall runner <name>,
  install <provider> on <host> [default <mode>],
  uninstall <provider> on <host>, uninstall for <mode> on <host>.
  Anything else yields a usage error (exit 2, message contains "usage:").
- Assets: INTEGRATIONS_DIR/<host>-host/skills/pilot-workers/ is installed
  host-level by install_host(host, target) -> {"files", "created_dirs"}.
- Manifest v4: {"schema_version": 4, "installs": {"<host>": {...}}} where a
  host entry may carry "providers" (visibility list) and "modes"
  (mode -> provider). A plain host reinstall preserves both. Installing over
  a v1/v2 manifest purges every legacy entry for that host (one printed
  "removed" line per removed file) and the on-disk file is rewritten as v4.
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
# grammar: provider-on-host forms are accepted
# ----------------------------------------------------------------------


def test_install_provider_on_host_accepted(isolated):
    rc = install_mod.main(["glm", "on", "claude"])
    assert rc == 0
    entry = _read_manifest()["installs"]["claude"]
    assert entry["providers"] == ["glm"]


def test_uninstall_provider_on_host_accepted(isolated):
    assert install_mod.main(["glm", "on", "claude"]) == 0
    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 0
    # That was the host's last provider, so the entry goes with the skill —
    # and an empty manifest is deleted outright.
    assert not _manifest_path().exists()


def test_uninstall_accepts_provider_absent_from_registry(isolated, monkeypatch):
    """Deleting a provider YAML must not strand its recorded entry.

    The uninstall parser classifies ``<x> on <host>`` by shape, not by the
    registry — otherwise the only command that could clean up the entry would
    reject the very key it needs to remove.
    """
    assert install_mod.main(["glm", "on", "claude", "for", "code"]) == 0
    monkeypatch.delitem(providers.PROVIDERS, "glm")

    assert install_mod.uninstall_main(["glm", "on", "claude"]) == 0

    # Last provider gone: the whole entry is removed, stranding nothing.
    assert not _manifest_path().exists()


def test_install_still_validates_provider_against_registry(isolated, capsys):
    """Only the removal direction is permissive; adding stays validated —
    and the error names the token rather than dumping the usage."""
    assert install_mod.main(["nope", "on", "claude"]) == 2
    err = capsys.readouterr().err
    assert "nope" in err and "unknown provider" in err


# ----------------------------------------------------------------------
# grammar: usage errors
# ----------------------------------------------------------------------


def test_install_skill_on_host_is_usage_error(isolated, capsys):
    rc = install_mod.main(
        ["skill", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 2
    assert "skill" in capsys.readouterr().err


def test_install_unknown_host_is_usage_error(isolated, capsys):
    """One bare token can be a mistyped host or a provider missing `on <host>`,
    so the message names both dimensions rather than only the host."""
    rc = install_mod.main(["bogus", "--target", str(isolated["target"])])
    assert rc == 2
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "claude" in err and "<provider> on <host>" in err


def test_grammar_error_empty_argv(isolated, capsys):
    rc = install_mod.main([])
    assert rc == 2
    # Rejection must be actionable; a naming error is better than a usage dump.
    assert capsys.readouterr().err.startswith("error:")


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
# manifest v4
# ----------------------------------------------------------------------


def test_install_claude_writes_v4_manifest(isolated):
    # A skill deploys only where a worker is configured.
    assert install_mod.main(
        ["glm", "on", "claude", "--target", str(isolated["target"])]) == 0
    rc = install_mod.main(["claude", "--target", str(isolated["target"])])
    assert rc == 0

    manifest = _read_manifest()
    assert manifest["schema_version"] == 4
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


def test_install_all_installs_both_hosts_v4(isolated):
    target = str(isolated["target"])
    for host in providers.HOSTS:
        assert install_mod.main(["glm", "on", host, "--target", target]) == 0
    rc = install_mod.main(["all", "--target", target])
    assert rc == 0

    manifest = _read_manifest()
    assert manifest["schema_version"] == 4
    installs = manifest["installs"]
    assert set(installs) == set(providers.HOSTS)
    for host in providers.HOSTS:
        entry = installs[host]
        assert entry["files"]
        for key in ("glm", "kimi-k3", "ds", "__all__"):
            assert key not in entry


def test_reinstall_purges_previous_host_install(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
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


def test_v2_manifest_migrated_to_v4_on_install(isolated, capsys):
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
    # Legacy artifacts of the removed architecture are cleaned, but with no
    # worker configured nothing is deployed in their place.
    assert "claude" not in _read_manifest().get("installs", {})
    assert "on claude" in out

    # The on-disk manifest is v4: no "hosts" key, no provider nesting, and the
    # legacy host entry is gone rather than rewritten.
    manifest = _read_manifest()
    assert manifest["schema_version"] == 4
    assert "hosts" not in manifest
    assert "claude" not in manifest["installs"]


def test_v1_manifest_migrated_to_v4_on_install(isolated, capsys):
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
    assert manifest["schema_version"] == 4
    assert "hosts" not in manifest
    # No worker configured: the legacy entry is gone and nothing replaces it.
    assert "claude" not in manifest.get("installs", {})
    assert "on claude" in out


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def test_uninstall_claude_v4_removes_files_and_deletes_manifest(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["glm", "on", "claude", "--target", target]) == 0
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


# ----------------------------------------------------------------------
# manifest schema v4: in-memory v3 -> v4 migration
# ----------------------------------------------------------------------


def _write_v3_manifest(installs: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 3,
        "installs": installs,
    }), encoding="utf-8")


def test_v3_manifest_loads_with_empty_config_and_is_not_rewritten(isolated):
    entry = {
        "installed_at": "2026-07-24T00:00:00+00:00",
        "package_version": "0.5.0",
        "files": ["/tmp/x/skills/pilot-workers/SKILL.md"],
        "created_dirs": [],
    }
    _write_v3_manifest({"claude": entry})
    on_disk_before = _manifest_path().read_text(encoding="utf-8")

    manifest = install_mod._load_manifest(_manifest_path())

    loaded = manifest["installs"]["claude"]
    assert manifest["schema_version"] == 4
    assert loaded["providers"] == []
    assert loaded["modes"] == {}
    # Load is read-only: the on-disk file is untouched.
    assert _manifest_path().read_text(encoding="utf-8") == on_disk_before


# ----------------------------------------------------------------------
# install <provider> on <host> [default <mode>]
# ----------------------------------------------------------------------


def test_install_provider_on_host_records_provider(isolated, capsys):
    rc = install_mod.main(["glm", "on", "claude"])
    assert rc == 0
    assert "glm" in capsys.readouterr().out

    manifest = _read_manifest()
    assert manifest["schema_version"] == 4
    entry = manifest["installs"]["claude"]
    assert entry["providers"] == ["glm"]
    assert entry["modes"] == {}
    # The skill exists wherever a provider is configured, so the deploy is
    # recorded too — that is what lets uninstall purge it later.
    assert entry["files"]


def test_install_provider_on_host_default_records_both(isolated):
    rc = install_mod.main(["glm", "on", "claude", "for", "code"])
    assert rc == 0

    entry = _read_manifest()["installs"]["claude"]
    # A default implies visibility: glm lands in providers even though only
    # `default` was named.
    assert entry["providers"] == ["glm"]
    assert entry["modes"] == {"code": "glm"}


def test_reinstall_preserves_providers_and_defaults(isolated):
    target = str(isolated["target"])
    assert install_mod.main(
        ["glm", "on", "claude", "for", "code", "--target", target]) == 0
    before = _read_manifest()["installs"]["claude"]
    assert before["providers"] == ["glm"]
    assert before["modes"] == {"code": "glm"}
    first_installed_at = before["installed_at"]

    assert install_mod.main(["claude", "--target", target]) == 0

    after = _read_manifest()["installs"]["claude"]
    assert after["providers"] == ["glm"]
    assert after["modes"] == {"code": "glm"}
    # Bookkeeping still refreshes.
    assert after["installed_at"] >= first_installed_at
    assert after["files"]


def test_hosts_stay_independent(isolated):
    assert install_mod.main(["glm", "on", "claude", "for", "code"]) == 0
    assert install_mod.main(["ds", "on", "codex"]) == 0

    installs = _read_manifest()["installs"]
    assert installs["claude"]["providers"] == ["glm"]
    assert installs["claude"]["modes"] == {"code": "glm"}
    assert installs["codex"]["providers"] == ["ds"]
    assert installs["codex"]["modes"] == {}


# ----------------------------------------------------------------------
# uninstall <provider> on <host> / uninstall for <mode> on <host>
# ----------------------------------------------------------------------


def test_uninstall_provider_on_host_removes_provider_and_defaults(isolated):
    assert install_mod.main(["glm", "on", "claude", "for", "code"]) == 0
    assert install_mod.main(["glm", "on", "claude", "for", "review"]) == 0
    assert install_mod.main(["ds", "on", "claude"]) == 0

    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 0

    entry = _read_manifest()["installs"]["claude"]
    assert entry["providers"] == ["ds"]
    # No default survives pointing at an invisible provider.
    assert entry["modes"] == {}


def test_uninstall_default_on_host_drops_only_that_default(isolated):
    assert install_mod.main(["glm", "on", "claude", "for", "code"]) == 0
    assert install_mod.main(["glm", "on", "claude", "for", "review"]) == 0

    rc = install_mod.uninstall_main(["for", "code", "on", "claude"])
    assert rc == 0

    entry = _read_manifest()["installs"]["claude"]
    # The provider stays visible; only the named default is dropped.
    assert entry["providers"] == ["glm"]
    assert entry["modes"] == {"review": "glm"}


def test_uninstall_provider_without_manifest_returns_1(isolated, capsys):
    rc = install_mod.uninstall_main(["glm", "on", "claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err


# ----------------------------------------------------------------------
# config accessors
# ----------------------------------------------------------------------


def test_remove_and_clear_tolerate_provider_absent_from_registry(isolated):
    installs = {"claude": {
        "providers": ["retired"],
        "modes": {"code": "retired", "review": "glm"},
    }}
    # `retired` is not in providers.PROVIDERS; removal must still work.
    assert install_mod.remove_host_provider(installs, "claude", "retired") is True
    assert installs["claude"]["providers"] == []
    assert installs["claude"]["modes"] == {"review": "glm"}
    assert install_mod.clear_host_mode(installs, "claude", "review") is True
    assert installs["claude"]["modes"] == {}
    # Idempotent: nothing left to change.
    assert install_mod.remove_host_provider(installs, "claude", "retired") is False
    assert install_mod.clear_host_mode(installs, "claude", "review") is False


def test_add_host_provider_rejects_unknown_provider(isolated):
    with pytest.raises(RuntimeError, match="unknown provider"):
        install_mod.add_host_provider({}, "claude", "bogus")


def test_set_host_mode_rejects_resume(isolated):
    with pytest.raises(RuntimeError, match="cannot assign mode"):
        install_mod.set_host_mode({}, "claude", "resume", "glm")


def test_set_host_mode_rejects_unknown_mode(isolated):
    with pytest.raises(RuntimeError, match="cannot assign mode"):
        install_mod.set_host_mode({}, "claude", "bogus", "glm")


def test_set_host_mode_rejects_unknown_provider(isolated):
    with pytest.raises(RuntimeError, match="unknown provider"):
        install_mod.set_host_mode({}, "claude", "code", "bogus")


def test_host_providers_and_defaults_for_missing_host(isolated):
    assert install_mod.host_providers({}, "claude") == []
    assert install_mod.host_modes({}, "claude") == {}


def test_install_provider_on_all_is_usage_error(isolated, capsys):
    rc = install_mod.main(["glm", "on", "all"])
    assert rc == 2
    # Rejection must be actionable; a naming error is better than a usage dump.
    assert capsys.readouterr().err.startswith("error:")


def test_uninstall_provider_on_all_is_usage_error(isolated, capsys):
    rc = install_mod.uninstall_main(["glm", "on", "all"])
    assert rc == 2
    # Rejection must be actionable; a naming error is better than a usage dump.
    assert capsys.readouterr().err.startswith("error:")


def test_install_unknown_provider_on_host_is_error(isolated, capsys):
    rc = install_mod.main(["bogus", "on", "claude"])
    assert rc == 2
    # Rejection must be actionable; a naming error is better than a usage dump.
    assert capsys.readouterr().err.startswith("error:")


def test_install_provider_default_resume_is_error(isolated, capsys):
    rc = install_mod.main(["glm", "on", "claude", "for", "resume"])
    assert rc == 1
    assert "resume" in capsys.readouterr().err


def test_install_provider_default_unknown_mode_is_error(isolated, capsys):
    rc = install_mod.main(["glm", "on", "claude", "for", "bogus"])
    assert rc == 1
    assert "cannot assign mode" in capsys.readouterr().err


def test_for_is_a_reserved_provider_key():
    assert "for" in providers.RESERVED_PROVIDER_KEYS


@pytest.mark.parametrize("word", ["for", "all", "on", "codex"])
def test_uninstall_reserved_word_on_host_is_usage_error(isolated, capsys, word):
    """A mode-less ``uninstall default on <host>`` must not read as a provider.

    The uninstall parser classifies ``<x> on <host>`` by shape; without the
    reserved-word guard a forgotten mode silently succeeded with a misleading
    "not recorded" note instead of a usage error.
    """
    assert install_mod.uninstall_main([word, "on", "claude"]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_reinstalling_same_provider_reports_no_change(isolated, capsys):
    assert install_mod.main(["glm", "on", "claude"]) == 0
    capsys.readouterr()
    assert install_mod.main(["glm", "on", "claude"]) == 0
    assert "already recorded" in capsys.readouterr().out


# ----------------------------------------------------------------------
# A corrupt manifest must be NAMED, not tripped over.
#
# _load_manifest already reports two corruption shapes cleanly ("corrupt
# install manifest ...": undecodable JSON, and a top-level that is not an
# object). Four more got through: a non-object `installs` and a non-dict
# `providers`/`modes` reached the accessors, where `list("glm")` silently
# became the provider keys ['g','l','m'] and `dict("glm")` raised ValueError
# out of `status` as an uncaught traceback.
# ----------------------------------------------------------------------

CORRUPT_MANIFESTS = [
    ('{"installs": "oops"}', "installs"),
    ('{"installs": {"claude": {"files": [], "providers": "glm", "modes": {}}}}',
     "providers"),
    ('{"installs": {"claude": {"files": [], "providers": 5, "modes": {}}}}',
     "providers"),
    ('{"installs": {"claude": {"files": [], "providers": ["glm"], "modes": "glm"}}}',
     "modes"),
    ('{"installs": {"claude": {"files": [], "providers": [1], "modes": {}}}}',
     "providers"),
    ('{"installs": {"claude": {"files": [], "providers": [], "modes": {"code": 7}}}}',
     "modes"),
]


@pytest.mark.parametrize("text,field", CORRUPT_MANIFESTS)
def test_a_corrupt_manifest_is_reported_not_tripped_over(isolated, text, field):
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt install manifest") as excinfo:
        install_mod._load_manifest(path)
    assert field in str(excinfo.value), (
        f"the error does not say which field is wrong: {excinfo.value}")


@pytest.mark.parametrize("text,field", CORRUPT_MANIFESTS)
def test_status_exits_cleanly_on_a_corrupt_manifest(isolated, capsys, text, field):
    """The symptom that surfaced this: `status` caught only OSError and
    RuntimeError, so AttributeError/TypeError/ValueError became tracebacks."""
    from pilot_workers.cli import status as status_mod

    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert status_mod.main([]) == 1
    err = capsys.readouterr().err
    assert "corrupt install manifest" in err, err


def test_a_v1_manifest_still_migrates(isolated):
    """The validation must not reject the legacy shapes it exists to migrate:
    a v1 entry has no providers/modes at all."""
    _write_v1_manifest({"claude": {"files": ["a"], "installed_at": "x"}})
    data = install_mod._load_manifest(_manifest_path())
    assert "claude" in data["installs"]


def test_a_valid_v4_manifest_is_unchanged_by_the_validation(isolated):
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
        install_mod.set_host_mode(installs, "claude", "code", "glm")
    data = install_mod._load_manifest(_manifest_path())
    assert data["installs"]["claude"]["providers"] == ["glm"]
    assert data["installs"]["claude"]["modes"] == {"code": "glm"}


def test_a_v1_manifest_with_a_non_object_hosts_is_reported(isolated):
    """The v1 migration runs BEFORE the installs validation, so a non-object
    `hosts` reached `.items()` and surfaced as AttributeError."""
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hosts": "oops"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="corrupt install manifest") as excinfo:
        install_mod._load_manifest(path)
    assert "hosts" in str(excinfo.value)


def test_status_exits_cleanly_on_a_non_object_hosts(isolated, capsys):
    from pilot_workers.cli import status as status_mod

    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hosts": "oops"}', encoding="utf-8")
    assert status_mod.main([]) == 1
    assert "corrupt install manifest" in capsys.readouterr().err


# ----------------------------------------------------------------------
# The orphan sweep must stay inside this host's skill directory.
#
# The paths come from the manifest, and the sweep unlinked whatever was listed:
# verified by putting an unrelated file in `files` and watching an install
# delete it. Not an attack — the manifest is the user's own private file — but a
# hand edit, a manifest copied between machines, or a path that has come to mean
# something else all end in a deleted file that was never ours.
# ----------------------------------------------------------------------

def test_the_orphan_sweep_refuses_a_path_outside_the_skill_directory(
        isolated, capsys):
    victim = isolated["home"] / "precious.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("do not delete me", encoding="utf-8")
    target = isolated["target"]

    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
        installs["claude"]["files"] = [str(victim)]

    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0
    assert victim.is_file(), "an install deleted a file outside its own tree"
    assert "refusing to remove" in capsys.readouterr().err


def test_the_orphan_sweep_still_removes_a_file_the_package_dropped(
        isolated, capsys):
    """Reverse assertion: the containment check must not disable the sweep."""
    target = isolated["target"]
    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0
    skill_dir = install_mod.install_host_destination("claude", target)
    stale = skill_dir / "REFERENCE-from-an-older-version.md"
    stale.write_text("old doctrine", encoding="utf-8")
    with install_mod.manifest_transaction() as installs:
        installs["claude"]["files"] = sorted(
            set(installs["claude"]["files"]) | {str(stale)})

    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0
    assert not stale.exists(), "a file the package no longer ships was left behind"
    assert "removed stale" in capsys.readouterr().out


def test_a_second_target_is_refused_rather_than_orphaning_the_first(
        isolated, capsys):
    """Why the orphan sweep never has to delete across locations: moving a
    deployment is refused outright, so the only paths it can legitimately remove
    are inside the one recorded skill directory."""
    first = isolated["target"]
    second = isolated["home"].parent / "target2"
    assert install_mod.main(["glm", "on", "claude", "--target", str(first)]) == 0
    old_skill = install_mod.install_host_destination("claude", first) / "SKILL.md"
    assert old_skill.is_file()

    assert install_mod.main(["glm", "on", "claude", "--target", str(second)]) != 0
    assert "would orphan it" in capsys.readouterr().err
    assert old_skill.is_file(), "the refusal did not leave the first deployment intact"


@pytest.mark.parametrize("argv", [
    ["glm", "on", "claude", "--target="],
    ["glm", "on", "claude", "--target", ""],
    ["claude", "--target="],
])
def test_an_empty_target_is_a_usage_error(isolated, capsys, argv):
    """Path("") resolves to the current working directory, so `--target=` used
    to deploy the skill tree wherever the user happened to be standing."""
    assert install_mod.main(argv) == 2
    assert "--target requires" in capsys.readouterr().err


def test_a_refresh_keeps_the_record_of_directories_the_first_install_made(isolated):
    """A refresh creates nothing (the dirs exist), so overwriting created_dirs
    dropped the first install's record — and uninstall only removes directories
    it is recorded as having created, so they were left behind forever."""
    target = isolated["target"]
    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0
    first = set(_read_manifest()["installs"]["claude"]["created_dirs"])
    assert first, "the first install recorded no created directories"

    assert install_mod.main(["glm", "on", "claude", "--target", str(target)]) == 0
    after = set(_read_manifest()["installs"]["claude"]["created_dirs"])
    assert first <= after, f"a refresh forgot {first - after}"


def test_uninstalling_after_a_refresh_removes_the_skill_directory(isolated):
    """The consequence, end to end."""
    target = isolated["target"]
    install_mod.main(["glm", "on", "claude", "--target", str(target)])
    install_mod.main(["glm", "on", "claude", "--target", str(target)])
    skill_dir = install_mod.install_host_destination("claude", target)
    assert skill_dir.is_dir()
    assert install_mod.uninstall_main(["claude"]) == 0
    assert not skill_dir.exists(), "the skill directory outlived its uninstall"


@pytest.mark.parametrize("mode", ["cod", "", "REVIEW", "resume"])
def test_uninstalling_an_unknown_mode_assignment_is_an_error(isolated, capsys, mode):
    """A typo printed 'no provider assigned to mode' and exited 0, which reads
    as done while the assignment the user meant to remove is still there."""
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    assert install_mod.uninstall_main(["for", mode, "on", "claude"]) != 0
    assert install_mod.host_modes(
        install_mod._load_manifest(_manifest_path())["installs"],
        "claude") == {"code": "glm"}, "the real assignment was disturbed"


# ----------------------------------------------------------------------
# A mode name in the provider slot is an omitted `for`, not a provider.
#
# `uninstall code on claude` matched the provider SHAPE, went to the provider
# handler, printed "code is not recorded on claude" and exited 0 — with the
# code -> glm assignment untouched. The comment above looks_like_provider_form
# already anticipates this class for reserved words; mode names were simply not
# in the reserved list.
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_a_mode_in_the_provider_slot_is_rejected_with_the_right_form(
        isolated, capsys, mode):
    install_mod.main(["glm", "on", "claude", "for", mode,
                      "--target", str(isolated["target"])])

    assert install_mod.uninstall_main([mode, "on", "claude"]) == 2
    err = capsys.readouterr().err
    assert f"uninstall for {mode} on claude" in err, err
    assert install_mod.host_modes(
        install_mod._load_manifest(_manifest_path())["installs"],
        "claude") == {mode: "glm"}, "the assignment was disturbed"


def test_the_correct_mode_form_still_works(isolated):
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    assert install_mod.uninstall_main(["for", "code", "on", "claude"]) == 0
    assert install_mod.host_modes(
        install_mod._load_manifest(_manifest_path())["installs"], "claude") == {}


def test_a_provider_that_happens_to_be_named_like_a_mode_still_uninstalls(
        isolated, monkeypatch):
    """Nothing reserves a mode name, so a provider MAY be called `code`. The
    rejection must not strand such an entry — only the removal direction is
    permissive, and that is the whole reason it is."""
    from dataclasses import replace

    monkeypatch.setitem(providers.PROVIDERS, "code",
                        replace(providers.PROVIDERS["glm"], key="code"))
    assert install_mod.main(["code", "on", "claude",
                             "--target", str(isolated["target"])]) == 0
    assert install_mod.uninstall_main(["code", "on", "claude"]) == 0
    assert not _manifest_path().exists()


def test_the_purge_refuses_a_recorded_path_it_never_deployed(isolated, capsys):
    """The round-11 containment check went to the orphan sweep and left this
    sibling deleting whatever the manifest listed — including anything else in
    the user's own ~/.claude."""
    victim = isolated["home"] / "precious.txt"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("do not delete me", encoding="utf-8")

    install_mod._purge_entry({"files": [str(victim)], "created_dirs": []})

    assert victim.is_file(), "the purge deleted a file outside any deploy dir"
    assert "refusing to remove" in capsys.readouterr().err


@pytest.mark.parametrize("rel", [
    "skills/pilot-workers/SKILL.md",
    "agents/worker-glm-code.md",
    "commands/pilot-review.md",
])
def test_the_purge_still_removes_what_this_tool_deploys(isolated, rel):
    """Reverse assertion, covering the v4 location and both v0.4.0 ones — a
    guard that blocked the legacy purge would strand exactly the files the
    migration exists to remove."""
    path = isolated["target"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("deployed", encoding="utf-8")

    install_mod._purge_entry({"files": [str(path)], "created_dirs": []})
    assert not path.exists(), f"{rel} was not purged"


def test_no_manifest_driven_removal_path_touches_an_outside_path():
    """Every place recorded manifest paths drive a removal, exercised.

    A source-level guard was tried twice and abandoned: function-scoped
    presence was too loose (it stayed green when the created_dirs containment
    was reverted, the exact miss it was written for), and node-scoped substring
    matching was too noisy (`host_entry.get("files")` matched as
    `entry.get("files"`). The property needs dataflow, so it is asserted
    behaviourally instead — one case per path, each with an out-of-tree victim.

    Honest limitation: this cannot catch a FOURTH path added later. The
    syntactically-local sibling of this idea does work — see
    test_no_mtime_read_in_this_module_is_unguarded in test_maintain.py, which
    found a real unguarded site the first time it ran.
    """
    import tempfile

    def victim(name):
        path = Path(tempfile.mkdtemp()) / name
        path.write_text("do not delete me", encoding="utf-8")
        return path

    # 1. the recorded `files` list
    f = victim("recorded-file.txt")
    install_mod._purge_entry({"files": [str(f)], "created_dirs": []})
    assert f.is_file(), "the files loop deleted an outside path"

    # 2. the recorded `created_dirs` list — the dir itself and its tmp glob
    d = victim("in-a-recorded-dir.txt")
    stale = d.parent / ".skill.abc.tmp"
    stale.write_text("x", encoding="utf-8")
    install_mod._purge_entry({"files": [], "created_dirs": [str(d.parent)]})
    assert stale.is_file(), "the created_dirs tmp glob deleted an outside path"
    assert d.parent.is_dir(), "the created_dirs rmdir removed an outside dir"


def test_the_orphan_sweep_path_is_covered_by_its_own_test():
    """The third manifest-driven removal path. Named here so the set of paths is
    visible in one place; the assertion lives in
    test_the_orphan_sweep_refuses_a_path_outside_the_skill_directory."""
    assert callable(install_mod._deploy_skill_tree)


def test_the_containment_helper_actually_refuses_something():
    """Reverse assertion: the guard above would pass just as well if the helper
    were `return True`."""
    from pathlib import Path

    assert install_mod._looks_deployed_by_us(
        Path("/home/u/.claude/skills/pilot-workers/SKILL.md"))
    assert install_mod._looks_deployed_by_us(Path("/t/agents/worker-x.md"))
    assert not install_mod._looks_deployed_by_us(Path("/home/u/.claude/CLAUDE.md"))
    assert not install_mod._looks_deployed_by_us(Path("/home/u/precious.txt"))


@pytest.mark.parametrize("relative", [
    "../../CLAUDE.md",
    "../../settings.json",
    "sub/../../../victim.txt",
])
def test_the_containment_check_is_not_fooled_by_dot_dot(relative):
    """Both variants were purely lexical: `Path.parents` does not resolve `..`
    and the name check still sees "pilot-workers" in the prefix, while os.unlink
    resolves it and removes the file two levels up. The docstring claimed true
    containment and did not have it."""
    skill_dir = Path("/home/u/.claude/skills/pilot-workers")
    escaping = skill_dir / relative
    assert not install_mod._looks_deployed_by_us(escaping)
    assert not install_mod._looks_deployed_by_us(escaping, {skill_dir})


def test_a_real_path_inside_the_skill_dir_still_passes():
    """Reverse assertion: normalising must not refuse ordinary paths."""
    skill_dir = Path("/home/u/.claude/skills/pilot-workers")
    for rel in ("SKILL.md", "sub/REFERENCE.md", "./SKILL.md"):
        assert install_mod._looks_deployed_by_us(skill_dir / rel)
        assert install_mod._looks_deployed_by_us(skill_dir / rel, {skill_dir})


def test_a_traversal_path_is_not_deleted_end_to_end(isolated, capsys):
    """The consequence, driven through the purge rather than the helper."""
    import tempfile

    outside = Path(tempfile.mkdtemp())
    victim = outside / "CLAUDE.md"
    victim.write_text("user's own file", encoding="utf-8")
    skill_dir = outside / "skills" / "pilot-workers"
    skill_dir.mkdir(parents=True)

    install_mod._purge_entry(
        {"files": [str(skill_dir / ".." / ".." / "CLAUDE.md")], "created_dirs": []})

    assert victim.is_file(), "a `..` path escaped the containment check"
    assert "refusing to remove" in capsys.readouterr().err


def test_a_created_dir_that_fails_containment_says_so(isolated, capsys):
    """It was dropped silently, leaving a directory nothing would ever remove
    and no reason given — while the files loop next to it printed a note."""
    install_mod._purge_entry({"files": [], "created_dirs": ["/tmp/not-ours"]})
    assert "refusing to remove /tmp/not-ours" in capsys.readouterr().err


def test_reinstalling_repairs_a_host_that_has_providers_but_no_skill(
        isolated, capsys):
    """`skill_missing` only covered recorded-but-gone. A host entry with
    providers and no files list (hand-edited, or copied between machines) hit the
    early return, printed "already recorded" and exited 0 without deploying —
    leaving a host that cannot delegate and no indication anything was wrong."""
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
    assert "files" not in install_mod._load_manifest(
        _manifest_path())["installs"]["claude"]

    # NO --target: the early return is gated on `target is None`, so passing one
    # bypasses the branch under test entirely. The first version of this test did
    # pass it and proved nothing. conftest redirects HOME, so the default
    # location is inside the sandbox.
    rc = install_mod.main(["glm", "on", "claude"])
    assert rc == 0
    capsys.readouterr()
    # The signal is the DEPLOY, not the message: "already recorded" is printed
    # after a successful repair too (the PROVIDER was already recorded, which is
    # true). Asserting on the message proved nothing — it failed against the
    # fixed and the unfixed code alike.
    skill = install_mod.install_host_destination("claude", None) / "SKILL.md"
    assert skill.is_file(), "the repair did not deploy the skill"
    assert install_mod._load_manifest(
        _manifest_path())["installs"]["claude"]["files"]


def test_a_fully_installed_host_still_reports_already_recorded(isolated, capsys):
    """Reverse assertion: the early return must survive for the case it is for."""
    target = isolated["target"]
    install_mod.main(["glm", "on", "claude", "--target", str(target)])
    capsys.readouterr()
    assert install_mod.main(["glm", "on", "claude"]) == 0
    assert "already recorded" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["glm", "on", "claude", "--target", "--global-key"],
    ["glm", "on", "claude", "--target=--global-key"],
    ["claude", "--target", "--target"],
])
def test_a_flag_is_not_a_target_directory(isolated, capsys, argv):
    """`--target --global-key` set the target to "--global-key" AND consumed the
    flag, so the user got neither a key prompt nor a usable target. The empty-value
    case was parametrised; this shape was not."""
    assert install_mod.main(argv) == 2
    assert "--target requires" in capsys.readouterr().err


def test_the_legacy_purge_is_not_handed_v4_worker_config(isolated):
    """`_host_purge_entries` skips `modes` and `providers` so the v4 worker config
    is never treated as a legacy sub-entry with files to unlink. Widened in round
    16 with no test."""
    entry = {
        "providers": ["glm"],
        "modes": {"code": "glm"},
        "__all__": {"files": [], "created_dirs": []},
    }
    purgeable = install_mod._host_purge_entries(entry)
    assert purgeable == [entry["__all__"]], purgeable
