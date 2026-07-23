"""Offline tests for pilot_workers.cli.install.

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


def test_install_claude_writes_manifest(isolated):
    rc = install_mod.main(["claude", "--target", str(isolated["target"])])
    assert rc == 0

    manifest_path = _manifest_path()
    assert manifest_path.is_file()
    manifest = _read_manifest()
    assert manifest["schema_version"] == 1
    entry = manifest["hosts"]["claude"]
    assert len(entry["files"]) == 20
    # Every recorded file path must exist on disk under the target.
    for name in entry["files"]:
        assert Path(name).is_file()
        assert Path(name).is_relative_to(isolated["target"].resolve())
    assert entry["created_dirs"]
    assert entry["installed_at"]
    assert entry["package_version"]


def test_reinstall_claude_purges_stale_files(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["claude", "--target", target]) == 0
    first_files = list(_read_manifest()["hosts"]["claude"]["files"])

    assert install_mod.main(["claude", "--target", target]) == 0
    out = capsys.readouterr().out
    assert "stale file" in out

    manifest = _read_manifest()
    second_files = manifest["hosts"]["claude"]["files"]
    assert len(second_files) == 20
    assert sorted(second_files) == sorted(first_files)

    # No duplicated files on disk: agents + commands stay at 20 total.
    installed = [p for p in Path(target).rglob("*.md") if p.is_file()]
    assert len(installed) == 20


def test_install_codex_records_skills(isolated):
    rc = install_mod.main(["codex", "--target", str(isolated["target"])])
    assert rc == 0

    entry = _read_manifest()["hosts"]["codex"]
    files = [Path(name) for name in entry["files"]]
    for skill in ("glm", "kimi", "ds"):
        assert any(skill in f.parts and f.name == "SKILL.md" for f in files), skill

    created_dirs = [Path(name) for name in entry["created_dirs"]]
    assert any(d.name in ("glm", "kimi", "ds") for d in created_dirs)


def test_uninstall_claude_removes_files_and_manifest_entry(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["all", "--target", target]) == 0
    claude_files = list(_read_manifest()["hosts"]["claude"]["files"])

    rc = install_mod.uninstall_main(["claude"])
    assert rc == 0

    # Every file recorded for claude is gone from disk.
    for name in claude_files:
        assert not Path(name).exists()

    manifest = _read_manifest()
    assert "claude" not in manifest["hosts"]
    assert "codex" in manifest["hosts"]


def test_uninstall_all_hosts_deletes_manifest_file(isolated):
    target = str(isolated["target"])
    assert install_mod.main(["all", "--target", target]) == 0
    assert _manifest_path().is_file()

    assert install_mod.uninstall_main(["all"]) == 0
    assert not _manifest_path().exists()


def test_uninstall_without_manifest_returns_1(isolated, capsys):
    rc = install_mod.uninstall_main(["claude"])
    assert rc == 1
    assert "no install manifest" in capsys.readouterr().err


def test_uninstall_host_missing_from_manifest_returns_1(isolated, capsys):
    target = str(isolated["target"])
    assert install_mod.main(["claude", "--target", target]) == 0

    rc = install_mod.uninstall_main(["codex"])
    assert rc == 1
    assert "no manifest entry" in capsys.readouterr().err
    # claude entry is untouched.
    assert "claude" in _read_manifest()["hosts"]
