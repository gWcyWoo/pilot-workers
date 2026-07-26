"""Offline tests for per-host worker config and divergence reporting in status.

The manifest records what install decided; the deployed files are what the
planner actually reads. They can drift (a hand-edited manifest, a deleted
provider YAML, a removed skill directory), so status must name the drift rather
than present a tidy picture that is not true.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.cli import install as install_mod
from pilot_workers.cli import status as status_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return {"home": tmp_path / "home", "target": tmp_path / "target"}


def _manifest_path(isolated):
    return isolated["home"] / "install-manifest.json"


def _patch_manifest(isolated, host, **changes):
    path = _manifest_path(isolated)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["installs"][host].update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


# ----------------------------------------------------------------------
# the config is reported
# ----------------------------------------------------------------------


def test_json_reports_per_host_providers_and_defaults(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["installs"]["claude"]["providers"] == ["glm"]
    assert data["installs"]["claude"]["modes"] == {"code": "glm"}


def test_human_output_shows_the_configured_default(isolated, capsys):
    install_mod.main(["ds", "on", "claude", "for", "explore",
                      "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "ds" in out and "explore" in out


def test_unconfigured_host_reports_empty_config(isolated, capsys):
    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["installs"]["codex"]["providers"] == []
    assert data["installs"]["codex"]["modes"] == {}


def test_host_detail_shows_the_config(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main(["claude"]) == 0
    out = capsys.readouterr().out
    assert "glm" in out and "code" in out


# ----------------------------------------------------------------------
# divergence is named, not hidden
# ----------------------------------------------------------------------


def test_providers_recorded_but_no_skill_deployed_is_flagged(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    # Someone deleted the deployed tree by hand.
    for path in (isolated["target"] / "skills" / "pilot-workers").rglob("*"):
        if path.is_file():
            path.unlink()
    capsys.readouterr()

    assert status_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "!!" in out


def test_divergence_appears_in_json(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    for path in (isolated["target"] / "skills" / "pilot-workers").rglob("*"):
        if path.is_file():
            path.unlink()
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["installs"]["claude"]["issues"]


def test_an_assignment_naming_an_unknown_provider_is_flagged(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    _patch_manifest(isolated, "claude",
                    providers=["gone"], modes={"code": "gone"})
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    issues = " ".join(data["installs"]["claude"]["issues"])
    assert "gone" in issues


def test_an_assignment_outside_the_visibility_list_is_flagged(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    _patch_manifest(isolated, "claude", modes={"code": "ds"})
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    issues = " ".join(data["installs"]["claude"]["issues"])
    assert "ds" in issues


def test_a_deployed_skill_with_nothing_recorded_is_flagged(isolated, capsys,
                                                          monkeypatch, tmp_path):
    """The skill is written before the manifest commits.

    A crash in between leaves the planner reading a doctrine that advertises
    workers the manifest no longer records — and status used to report only the
    opposite direction, so it said nothing at all.
    """
    fake_home = tmp_path / "fakehome"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    orphan = fake_home / ".claude" / "skills" / "pilot-workers" / "SKILL.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("# leftover doctrine\n", encoding="utf-8")
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    issues = " ".join(data["installs"]["claude"]["issues"])
    assert "no provider is recorded" in issues


def test_an_orphan_at_a_custom_target_is_flagged(isolated, capsys):
    """The check looked only at the host's DEFAULT location, so a `--target`
    deployment whose providers were dropped reported a clean machine."""
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    _patch_manifest(isolated, "claude", providers=[], modes={})
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    issues = " ".join(data["installs"]["claude"]["issues"])
    assert "no provider is recorded" in issues
    assert str(isolated["target"]) in issues, "did not name the custom path"


def test_uninstalling_a_host_names_what_it_did_not_remove(isolated, capsys,
                                                          monkeypatch, tmp_path):
    """`uninstall <host>` removes that host's skill and manifest entry only.

    Keys, the runner binary, sandboxes, logs and worktrees are global by design
    — but a user who read "Done." had no way to discover they were still there.
    """
    from pilot_workers.runners import get_runner

    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    provider = providers.PROVIDERS["glm"]
    key_path = get_runner(provider.runner).credential_path(provider)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text('{"zhipu": {"type": "api", "key": "fake"}}', encoding="utf-8")
    providers.runs_root(provider).mkdir(parents=True, exist_ok=True)
    capsys.readouterr()

    assert install_mod.uninstall_main(["claude"]) == 0
    out = capsys.readouterr().out
    assert "uninstall key <provider>" in out
    assert "maintain runs" in out


def test_uninstalling_one_host_names_the_other_still_installed(isolated, capsys):
    """The most consequential survivor is a host that goes on routing work."""
    t = str(isolated["target"])
    install_mod.main(["glm", "on", "claude", "--target", t])
    install_mod.main(["ds", "on", "codex", "--target", t])
    capsys.readouterr()

    assert install_mod.uninstall_main(["claude"]) == 0
    out = capsys.readouterr().out
    assert "still installed: codex" in out


def test_uninstalling_the_last_host_names_no_survivor(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    capsys.readouterr()

    assert install_mod.uninstall_main(["claude"]) == 0
    assert "still installed" not in capsys.readouterr().out


def test_status_on_a_bare_machine_names_the_first_step(isolated, capsys):
    """`install` prints a next-step line; `status` printed none, so a fresh or
    freshly-wiped machine that asked status first had to guess."""
    assert status_mod.main([]) == 0
    out = capsys.readouterr().out
    assert "No worker is configured" in out
    assert "install <provider> on claude --global-key" in out


def test_the_first_step_hint_disappears_once_configured(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main([]) == 0
    assert "get started" not in capsys.readouterr().out


def test_a_healthy_install_reports_no_issues(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["installs"]["claude"]["issues"] == []


def test_healthy_human_output_has_no_warning_marker(isolated, capsys):
    install_mod.main(["glm", "on", "claude", "for", "code",
                      "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main([]) == 0
    assert "!!" not in capsys.readouterr().out


def test_the_per_host_json_form_carries_the_divergence_it_computes(isolated, capsys):
    """`status <host> --json` emitted only {host, installed, entry}.

    The human form prints every issue as `!! ...` and `status --json` includes
    them, so a machine consumer using the per-host form to detect drift was told
    a diverged host was fine.
    """
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    _patch_manifest(isolated, "claude", providers=["gone"], modes={"code": "gone"})
    capsys.readouterr()

    assert status_mod.main(["claude", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "issues" in data, "the per-host JSON form still drops the issue list"
    assert any("gone" in issue for issue in data["issues"])
    assert data["providers"] == ["gone"]
    assert data["modes"] == {"code": "gone"}


def test_a_healthy_host_reports_an_empty_issue_list_not_a_missing_key(isolated, capsys):
    """Absent vs empty matters to a consumer: one is 'no drift', the other is
    'this field does not exist and I cannot check'."""
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    capsys.readouterr()

    assert status_mod.main(["claude", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["issues"] == []
