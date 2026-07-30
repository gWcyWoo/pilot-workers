"""User-level permission overrides: the `permissions` CLI and its merge layer.

All tests isolate PILOT_WORKERS_HOME; nothing touches the real user config.
"""

from __future__ import annotations

import fnmatch
import json

import pytest

from pilot_workers import policy
from pilot_workers.cli import permissions as perms_cli
from pilot_workers.providers import PROVIDERS


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _verdict(cmd: str, rules: dict[str, str]) -> str:
    """Last matching pattern wins, as the runner's config encodes it."""
    verdict = "deny"
    for pattern, value in rules.items():
        if fnmatch.fnmatch(cmd, pattern):
            verdict = value
    return verdict


def _provider():
    return PROVIDERS["ds"]


# ----------------------------------------------------------------------
# add
# ----------------------------------------------------------------------


def test_add_creates_the_overrides_file_with_0600(isolated):
    assert perms_cli.main(["add", "ds", "explore", "apifox *"]) == 0
    path = policy.permission_overrides_path("ds")
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"explore": {"shell": {"apifox *": "allow"}}}


def test_added_rule_reaches_effective_permissions(isolated):
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    rules = policy.effective_permissions(_provider(), "explore")["bash"]
    assert _verdict("apifox run collection", rules) == "allow"
    # Other modes are untouched.
    code_rules = policy.effective_permissions(_provider(), "code")["bash"]
    assert _verdict("apifox run collection", code_rules) == "allow"  # code allows *
    review_rules = policy.effective_permissions(_provider(), "review")["bash"]
    assert _verdict("apifox run collection", review_rules) == "deny"


def test_add_reaches_build_config(isolated):
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    config = policy.build_config(_provider(), "explore")
    agent = config["agent"][policy.MODE_TO_AGENT["explore"]]
    assert agent["permission"]["bash"].get("apifox *") == "allow"


def test_add_all_section_applies_to_every_mode(isolated):
    perms_cli.main(["add", "ds", "_all", "apifox *"])
    for mode in policy.VALID_MODES:
        rules = policy.effective_permissions(_provider(), mode)["bash"]
        assert _verdict("apifox run x", rules) == "allow", mode


def test_added_allow_cannot_displace_the_redirect_deny(isolated):
    """The `*>*` guardrail is re-pinned after the override merge."""
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    rules = policy.effective_permissions(_provider(), "explore")["bash"]
    assert _verdict("apifox export > out.json", rules) == "deny"


def test_add_deny_action(isolated):
    perms_cli.main(["add", "ds", "explore", "git log*", "--action", "deny"])
    rules = policy.effective_permissions(_provider(), "explore")["bash"]
    assert _verdict("git log --oneline", rules) == "deny"


def test_re_adding_moves_the_rule_to_the_end(isolated):
    """Last-match-wins: the operator's latest word must win."""
    perms_cli.main(["add", "ds", "explore", "apifox *", "--action", "deny"])
    perms_cli.main(["add", "ds", "explore", "zzz *"])
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    data = json.loads(
        policy.permission_overrides_path("ds").read_text(encoding="utf-8"))
    assert list(data["explore"]["shell"]) == ["zzz *", "apifox *"]
    assert data["explore"]["shell"]["apifox *"] == "allow"


@pytest.mark.parametrize("argv,code", [
    (["add", "ghost", "explore", "x *"], 2),          # unknown provider
    (["add", "ds", "bogus", "x *"], 2),               # unknown mode
    (["add", "ds", "explore", "x *", "--action", "ask"], 2),  # unknown action
    (["add", "ds", "explore"], 2),                    # missing pattern
])
def test_add_rejects_bad_input(isolated, argv, code, capsys):
    assert perms_cli.main(argv) == code
    assert not policy.permission_overrides_path("ds").exists()


# ----------------------------------------------------------------------
# remove
# ----------------------------------------------------------------------


def test_remove_deletes_the_rule(isolated):
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    perms_cli.main(["add", "ds", "explore", "other *"])
    assert perms_cli.main(["remove", "ds", "explore", "apifox *"]) == 0
    data = json.loads(
        policy.permission_overrides_path("ds").read_text(encoding="utf-8"))
    assert "apifox *" not in data["explore"]["shell"]
    assert "other *" in data["explore"]["shell"]


def test_removing_the_last_rule_deletes_the_file(isolated):
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    assert perms_cli.main(["remove", "ds", "explore", "apifox *"]) == 0
    assert not policy.permission_overrides_path("ds").exists()


def test_remove_is_permissive(isolated, capsys):
    """No file, unknown provider, unknown mode: a note, never an error —
    removal must work even after the provider or mode left the registry."""
    assert perms_cli.main(["remove", "ghost", "explore", "x *"]) == 0
    assert perms_cli.main(["remove", "ds", "gone-mode", "x *"]) == 0
    out = capsys.readouterr().out
    assert out.count("note:") == 2


# ----------------------------------------------------------------------
# show
# ----------------------------------------------------------------------


def test_show_marks_override_rules(isolated, capsys):
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    capsys.readouterr()
    assert perms_cli.main(["show", "ds", "explore"]) == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "apifox" in l)
    assert "[override]" in line and "allow" in line


def test_show_unknown_provider_is_an_error(isolated, capsys):
    assert perms_cli.main(["show", "ghost"]) == 2


def test_show_all_modes_when_mode_omitted(isolated, capsys):
    assert perms_cli.main(["show", "ds"]) == 0
    out = capsys.readouterr().out
    for mode in policy.VALID_MODES:
        assert f"== {mode}" in out


# ----------------------------------------------------------------------
# load layer
# ----------------------------------------------------------------------


def test_corrupt_overrides_fail_loudly(isolated):
    path = policy.permission_overrides_path("ds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        policy.load_permission_overrides("ds")


def test_cli_reports_a_corrupt_file_without_a_traceback(isolated, capsys):
    path = policy.permission_overrides_path("ds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert perms_cli.main(["add", "ds", "explore", "x *"]) == 1
    assert perms_cli.main(["remove", "ds", "explore", "x *"]) == 1
    err = capsys.readouterr().err
    assert err.count("error:") == 2


def test_main_routes_the_permissions_subcommand(isolated, capsys):
    from pilot_workers.cli.main import main as cli_main

    assert cli_main(["permissions", "show", "ds"]) == 0
    assert "== explore" in capsys.readouterr().out


def test_overrides_with_bad_shape_fail_loudly(isolated):
    path = policy.permission_overrides_path("ds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"explore": "allow"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="must be a mapping"):
        policy.load_permission_overrides("ds")


def test_absent_overrides_are_none_and_change_nothing(isolated):
    assert policy.load_permission_overrides("ds") is None
    base = policy.agent_permissions("explore")["bash"]
    merged = policy.effective_permissions(_provider(), "explore")["bash"]
    assert list(base) == list(merged)


def test_unreadable_overrides_fail_as_runtime_error(isolated):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    path = policy.permission_overrides_path("ds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o000)
    try:
        with pytest.raises(RuntimeError, match="cannot read"):
            policy.load_permission_overrides("ds")
    finally:
        path.chmod(0o600)


# ----------------------------------------------------------------------
# review findings (kimi-k3, security axis): double-merge crash classes
# ----------------------------------------------------------------------


def test_scalar_bash_from_a_prior_layer_does_not_crash_the_second_merge(isolated):
    """A profile with `tools: {bash: deny}` leaves bash as a SCALAR via the
    early-return path; the overrides merge then called dict() on that string
    and raised ValueError — blocking every dispatch for the provider/mode.
    The synthesized map keeps the wholesale intent AND the mode's floor."""
    base = policy.agent_permissions("explore")
    first = policy._merge_permissions(
        base, {"explore": {"tools": {"bash": "deny"}}}, "explore")
    assert first["bash"]["*"] == "deny"
    second = policy._merge_permissions(
        first, {"_all": {"shell": {"apifox *": "allow"}}}, "explore")
    rules = second["bash"]
    assert _verdict("apifox run x", rules) == "allow"
    # The wholesale deny survives for everything else, and the read-only
    # mode's floor (redirects, credential paths) is carried into the seed.
    assert _verdict("ls -la", rules) == "deny"
    assert _verdict("apifox export > out.json", rules) == "deny"
    assert _verdict("cat .env", rules) == "deny"


def test_scalar_bash_seed_does_not_import_a_redirect_deny_into_code_mode(isolated):
    """The floor is per-mode: code legitimately allows redirects, so the
    synthesized seed must not smuggle `*>*` deny into a code merge."""
    base = policy.agent_permissions("code")
    first = policy._merge_permissions(
        base, {"code": {"tools": {"bash": "deny"}}}, "code")
    second = policy._merge_permissions(
        first, {"_all": {"shell": {"apifox *": "allow"}}}, "code")
    rules = second["bash"]
    assert _verdict("apifox run x", rules) == "allow"
    assert _verdict("apifox export > out.json", rules) == "allow"
    assert _verdict("cat .env", rules) == "deny"  # credential floor is universal


def test_show_renders_a_wholesale_bash_deny_without_crashing(isolated, capsys):
    path = policy.permission_overrides_path("ds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_all": {"tools": {"bash": "deny"}}}),
                    encoding="utf-8")
    assert perms_cli.main(["show", "ds", "explore"]) == 0
    out = capsys.readouterr().out
    assert '"*" deny' in out


def test_profile_plus_overrides_keep_guardrails_pinned(isolated):
    """The both-layers combination the change's safety rests on: a named
    profile (relaxed re-allows curl) AND a CLI-added override, with the
    credential/redirect denies still resolving last."""
    perms_cli.main(["add", "ds", "explore", "apifox *"])
    rules = policy.effective_permissions(
        _provider(), "explore", permission_profile="relaxed")["bash"]
    assert _verdict("apifox run x", rules) == "allow"
    assert _verdict("curl https://x", rules) == "allow"  # from the profile
    assert _verdict("apifox export > out.json", rules) == "deny"
    assert _verdict("cat .env", rules) == "deny"
    assert _verdict("cat auth.json", rules) == "deny"


def test_adding_an_allow_for_a_guardrail_pattern_warns(isolated, capsys):
    assert perms_cli.main(["add", "ds", "explore", "*>*"]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and "guardrail" in err
    # And the merge honours the deliberate override.
    rules = policy.effective_permissions(_provider(), "explore")["bash"]
    assert _verdict("echo hi > /tmp/x", rules) == "allow"
