"""Offline unit tests for pilot_workers.policy."""

from fnmatch import fnmatchcase

import pytest

from pilot_workers import policy
from pilot_workers.providers import PROVIDERS
from pilot_workers.policy import (
    MODE_TO_AGENT,
    STEPS_BY_MODE,
    _merge_permissions,
    agent_permissions,
    build_config,
    code_shell_permissions,
    load_permission_profile,
    load_prompt,
    readonly_shell_permissions,
)


def test_mode_to_agent_mapping():
    assert MODE_TO_AGENT == {
        "code": "worker-code",
        "explore": "worker-explore",
        "test": "worker-test",
        "review": "worker-review",
        "resume": "worker-code",
    }


def test_steps_by_mode_values():
    assert STEPS_BY_MODE == {
        "code": 120,
        "resume": 120,
        "review": 120,
        "explore": 80,
        "test": 80,
    }


def test_readonly_shell_permissions():
    rules = readonly_shell_permissions()
    assert rules["*"] == "deny"
    assert rules["rg *"] == "allow"
    assert rules["*>*"] == "deny"
    assert list(rules)[-1] == "*>*"


def _resolve(rules, command):
    """Mirror OpenCode's resolution: last-match-wins over insertion order."""
    action = None
    for pattern, value in rules.items():
        if fnmatchcase(command, pattern):
            action = value
    return action


def test_readonly_denies_awk_command_execution():
    rules = readonly_shell_permissions()
    assert _resolve(rules, "awk 'BEGIN{system(\"id\")}'") == "deny"
    assert _resolve(rules, "awk '{print}' file.txt") == "deny"
    assert _resolve(rules, "gawk 'BEGIN{\"id\" | getline}'") == "deny"


def test_readonly_denies_sed_command_execution():
    rules = readonly_shell_permissions()
    assert _resolve(rules, "sed 's/a/b/e' file.txt") == "deny"
    assert _resolve(rules, "sed -n '1,10p' file.txt") == "deny"
    assert _resolve(rules, "sed -i 's/a/b/' file.txt") == "deny"


def test_readonly_denies_find_exec_actions():
    rules = readonly_shell_permissions()
    assert _resolve(rules, "find . -name '*.py' -exec sh -c 'id' ;") == "deny"
    assert _resolve(rules, "find . -execdir id ;") == "deny"
    assert _resolve(rules, "find . -ok rm {} ;") == "deny"
    assert _resolve(rules, "find . -name '*.pyc' -delete") == "deny"
    assert _resolve(rules, "find . -fprintf /tmp/out '%p'") == "deny"
    assert _resolve(rules, "find . -fls /tmp/out") == "deny"
    assert _resolve(rules, "find . -name '*.py' -type f") == "allow"


def test_readonly_denies_interpreters_and_forwarders():
    rules = readonly_shell_permissions()
    for command in (
        "perl -e 'system(\"id\")'",
        "python -c 'import os; os.system(\"id\")'",
        "python3 evil.py",
        "ruby -e 'system(\"id\")'",
        "node -e 'require(\"child_process\").execSync(\"id\")'",
        "xargs -I{} sh -c '{}'",
        "sh -c id",
        "bash -c id",
        "zsh -c id",
    ):
        assert _resolve(rules, command) == "deny", command


def test_readonly_npx_only_runs_tsc_itself():
    rules = readonly_shell_permissions()
    assert _resolve(rules, "npx tsc") == "allow"
    assert _resolve(rules, "npx tsc --noEmit") == "allow"
    assert _resolve(rules, "npx tscmalicious-package") == "deny"


def test_readonly_denies_git_exec_and_write_flags():
    rules = readonly_shell_permissions()
    assert _resolve(rules, "git log --output=/tmp/exfil.txt") == "deny"
    assert _resolve(rules, "git grep -Osh pattern") == "deny"
    assert _resolve(rules, "git grep --open-files-in-pager=sh pattern") == "deny"
    assert _resolve(rules, "git log --oneline -5") == "allow"
    assert _resolve(rules, "git grep -n pattern") == "allow"


def test_readonly_keeps_safe_read_commands_allowed():
    rules = readonly_shell_permissions()
    for command in (
        "pwd",
        "ls -la src",
        "cat README.md",
        "rg -n pattern src",
        "grep -rn pattern src",
        "head -20 file.txt",
        "git diff --stat",
        "git status",
    ):
        assert _resolve(rules, command) == "allow", command


def test_test_mode_redirect_deny_stays_last():
    rules = policy.test_shell_permissions()
    assert list(rules)[-1] == "*>*"
    assert _resolve(rules, "pytest > /tmp/out.txt") == "deny"
    assert _resolve(rules, "npm test > /tmp/out.txt 2>&1") == "deny"


def test_test_mode_inherits_exec_denies_but_allows_runners():
    rules = policy.test_shell_permissions()
    assert _resolve(rules, "awk 'BEGIN{system(\"id\")}'") == "deny"
    assert _resolve(rules, "sed -i 's/a/b/' file.txt") == "deny"
    assert _resolve(rules, "python evil.py") == "deny"
    assert _resolve(rules, "pytest -x tests/") == "allow"
    assert _resolve(rules, "python -m pytest tests/") == "allow"
    assert _resolve(rules, "go test ./...") == "allow"


def test_code_shell_permissions():
    rules = code_shell_permissions()
    assert rules["*"] == "allow"
    assert rules["curl *"] == "deny"
    keys = list(rules)
    assert keys.index("curl *") > keys.index("*")


def test_test_shell_permissions():
    rules = policy.test_shell_permissions()
    assert rules["pytest*"] == "allow"
    assert rules["curl *"] == "deny"


def test_agent_permissions_code_allows_edit():
    assert agent_permissions("code")["edit"] == "allow"


def test_agent_permissions_explore_denies_edit():
    assert agent_permissions("explore")["edit"] == "deny"


def test_agent_permissions_resume_matches_code():
    assert agent_permissions("resume")["edit"] == "allow"


def test_load_permission_profile_relaxed():
    profile = load_permission_profile("relaxed")
    assert isinstance(profile, dict)
    assert "_all" in profile


def test_load_permission_profile_nonexistent_raises():
    with pytest.raises(RuntimeError, match="not found"):
        load_permission_profile("nonexistent")


def test_load_permission_profile_unknown_section(tmp_path, monkeypatch):
    (tmp_path / "bad.yaml").write_text("foo: {}\n", encoding="utf-8")
    monkeypatch.setattr(policy, "PERMISSIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="unknown section"):
        load_permission_profile("bad")


def test_merge_permissions_none_profile_returns_base():
    base = agent_permissions("code")
    assert _merge_permissions(base, None, "code") is base


def test_merge_permissions_all_shell_rules_override():
    base = agent_permissions("code")
    profile = {"_all": {"shell": {"curl *": "allow", "make *": "allow"}}}
    merged = _merge_permissions(base, profile, "code")
    assert merged["bash"]["curl *"] == "allow"
    assert merged["bash"]["make *"] == "allow"


def test_merge_permissions_all_tools_override_top_level():
    base = agent_permissions("code")
    profile = {"_all": {"tools": {"webfetch": "allow"}}}
    merged = _merge_permissions(base, profile, "code")
    assert merged["webfetch"] == "allow"


def test_merge_permissions_mode_section_only_when_matching():
    base = agent_permissions("code")
    profile = {"explore": {"tools": {"webfetch": "allow"}}}
    merged = _merge_permissions(base, profile, "code")
    assert merged["webfetch"] == "deny"
    base_explore = agent_permissions("explore")
    merged_explore = _merge_permissions(base_explore, profile, "explore")
    assert merged_explore["webfetch"] == "allow"


def test_merge_permissions_resume_uses_code_section():
    base = agent_permissions("resume")
    profile = {"code": {"tools": {"webfetch": "allow"}}}
    merged = _merge_permissions(base, profile, "resume")
    assert merged["webfetch"] == "allow"


def test_build_config_code_mode():
    config = build_config(PROVIDERS["glm"], "code")
    assert config["model"] == "glm-worker/glm-5.2"
    assert config["default_agent"] == "worker-code"
    assert config["share"] == "disabled"
    assert config["agent"]["worker-code"]["steps"] == 120


def test_build_config_with_relaxed_profile_allows_webfetch():
    config = build_config(PROVIDERS["glm"], "code", permission_profile="relaxed")
    perms = config["agent"]["worker-code"]["permission"]
    assert perms["webfetch"] == "allow"


def test_build_config_without_profile_denies_webfetch():
    config = build_config(PROVIDERS["glm"], "code")
    perms = config["agent"]["worker-code"]["permission"]
    assert perms["webfetch"] == "deny"


def test_load_prompt_code_and_resume():
    prompt = load_prompt("code")
    assert isinstance(prompt, str)
    assert prompt.strip()
    assert load_prompt("resume") == prompt
