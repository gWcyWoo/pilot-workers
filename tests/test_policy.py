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


def _tool_verdict(rules, path: str) -> str:
    """Last matching pattern wins, the same semantics as the shell matrix."""
    import fnmatch

    if isinstance(rules, str):
        return rules
    verdict = "deny"
    for pattern, action in rules.items():
        if fnmatch.fnmatch(path, pattern):
            verdict = action
    return verdict


def test_agent_permissions_code_allows_edit():
    assert _tool_verdict(agent_permissions("code")["edit"], "src/app.py") == "allow"


def test_agent_permissions_explore_denies_edit():
    assert agent_permissions("explore")["edit"] == "deny"


def test_agent_permissions_resume_matches_code():
    assert _tool_verdict(
        agent_permissions("resume")["edit"], "src/app.py") == "allow"


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


# ---------------------------------------------------------------------------
# The file tools need the same path denies as the shell
#
# Verified against a real worker: with `read` set to a bare "allow", `cat .env`
# was refused by the shell matrix while the read tool opened the same file and
# returned its contents. A shell-only deny is a facade.
# ---------------------------------------------------------------------------


CREDENTIAL_PATHS = [
    ".env",
    "config/.env",
    ".env.local",
    ".env.production",
    "some/nested/dir/.env.test",
    "auth.json",
    "data/opencode/auth.json",
]


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review", "resume"])
@pytest.mark.parametrize("path", CREDENTIAL_PATHS)
def test_no_mode_lets_the_read_or_edit_tool_open_a_credential_path(mode, path):
    """Enforced, not just configured: verified against a real worker.

    With `read` set to a bare "allow", `cat .env` was refused by the shell
    matrix while the read tool opened the same file and returned its contents.
    After this change the same dispatch reported the read tool refused, quoting
    the `*.env*` deny back.
    """
    rules = agent_permissions(mode)
    for tool in ("read", "edit"):
        assert _tool_verdict(rules[tool], path) == "deny", (
            f"{mode}: {tool} may open {path}")


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review", "resume"])
@pytest.mark.parametrize("path", CREDENTIAL_PATHS)
def test_the_grep_deny_is_configured_but_is_NOT_a_boundary(mode, path):
    """We configure it; the engine does not enforce it. Do not read more into
    this test than that.

    Verified against a real worker with the deny in place: a recursive grep
    still returned the decoy line from `.env`, because the pattern is matched
    against what the call names, not against every file the search walks. A
    shell `grep -rn SECRET .` reaches the same content and is `allow` too, so
    denying the native tool would cost the worker its main navigation tool and
    close nothing. The controls that DO hold are elsewhere: the worker is told
    not to repeat a credential it stumbles on (prompts/common.md), and
    `--worktree` materialises tracked files only, so a gitignored `.env` is
    absent from the workdir entirely.
    """
    rules = agent_permissions(mode)
    assert _tool_verdict(rules["grep"], path) == "deny", (
        "the deny is gone from the config; if that was deliberate, this test "
        "and the guarantee documented in prompts/common.md must change together")


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review", "resume"])
@pytest.mark.parametrize("path", ["src/app.py", "README.md", "tests/test_x.py"])
def test_ordinary_files_stay_readable_in_every_mode(mode, path):
    """The denies must not cost the worker the files it exists to work on —
    that would be the same defect with the sign flipped."""
    rules = agent_permissions(mode)
    assert _tool_verdict(rules["read"], path) == "allow"
    assert _tool_verdict(rules["grep"], path) == "allow"


def test_only_code_and_resume_can_edit_at_all():
    for mode in ("explore", "review", "test"):
        assert agent_permissions(mode)["edit"] == "deny"
    for mode in ("code", "resume"):
        assert _tool_verdict(
            agent_permissions(mode)["edit"], "src/app.py") == "allow"


# ----------------------------------------------------------------------
# A path-shaped deny must hold in EVERY mode.
#
# `*auth.json*` and `*.env*` match anywhere in the command, unlike every other
# rule here, which is anchored to a command name. Test mode appends its runner
# allows AFTER the deny block, so under last-match-wins those two denies went
# inert there: `pytest .env` resolved to allow in test mode while code and
# review both denied it. Same shape as the file-tool facade — a rule that is
# present, documented in prompts/common.md, and does nothing.
#
# The whole deny set must NOT be re-appended: `python*` is deliberately
# overridden by `python -m pytest*`, and moving it later breaks test mode.
# ----------------------------------------------------------------------

CREDENTIAL_PATH_COMMANDS = [
    "pytest .env",
    "pytest config/auth.json",
    "npm run build .env",
    "make test .env.production",
    "go test ./data/opencode/auth.json",
    "cat .env",
]


@pytest.mark.parametrize("command", CREDENTIAL_PATH_COMMANDS)
def test_no_mode_lets_the_shell_name_a_credential_path(command):
    for mode, rules in (
        ("review/explore", policy.readonly_shell_permissions()),
        ("test", policy.test_shell_permissions()),
        ("code", policy.code_shell_permissions()),
    ):
        assert _resolve(rules, command) == "deny", f"{mode}: {command}"


def test_test_mode_still_runs_its_runners_after_the_path_denies_move_last():
    """The reverse assertion: the fix must not deny ordinary test commands."""
    rules = policy.test_shell_permissions()
    for command in (
        "pytest -x tests/",
        "python -m pytest tests/test_env_loading.py",
        "npm run build",
        "go test ./...",
        "make check",
    ):
        assert _resolve(rules, command) == "allow", command


def test_the_redirect_deny_is_still_the_last_rule():
    """Re-appending the path denies must not displace `*>*`, which has to stay
    last of all or `pytest > out` reopens."""
    rules = policy.test_shell_permissions()
    assert list(rules)[-1] == "*>*"
    assert _resolve(rules, "pytest > /tmp/out.txt") == "deny"


def test_the_path_denies_are_one_list_not_two():
    """The re-appended patterns have to BE the ones in the deny block, or a
    future edit to one silently leaves the other behind."""
    denies = policy.denied_shell_patterns()
    for pattern in policy.CREDENTIAL_PATH_DENIES:
        assert denies.get(pattern) == "deny", pattern


# ----------------------------------------------------------------------
# Widening a file TOOL must not silently discard its PATH denies.
#
# `data/permissions/README.md` documents exactly `explore: tools: edit: allow`.
# Following it replaced the whole `{*: allow, *auth.json*: deny, *.env*: deny}`
# map with a bare "allow", reopening the credential-path bypass and making
# prompts/common.md's "denied at the permission layer, in every mode" false for
# that run — visible only by diffing the merged map.
#
# The line drawn: widening a TOOL is about capability and keeps the path floor;
# widening an explicit PATH pattern in `shell:` is a deliberate, named act and
# is still honoured (that is what the bundled `relaxed` profile does).
# ----------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["read", "edit", "grep"])
@pytest.mark.parametrize("section,mode", [("_all", "explore"), ("explore", "explore")])
def test_a_profile_widening_a_file_tool_keeps_the_credential_denies(
        tool, section, mode):
    merged = _merge_permissions(
        policy.agent_permissions(mode), {section: {"tools": {tool: "allow"}}}, mode)
    rules = merged[tool]
    assert isinstance(rules, dict), (
        f"{tool} became a bare {rules!r}: the path denies were discarded")
    for pattern in policy.CREDENTIAL_PATH_DENIES:
        assert rules.get(pattern) == "deny", f"{tool} no longer denies {pattern}"
    assert _resolve(rules, "src/main.py") == "allow", (
        "the widening itself was lost")
    assert _resolve(rules, ".env") == "deny"
    assert _resolve(rules, "data/opencode/auth.json") == "deny"


def test_a_profile_may_still_tighten_a_file_tool():
    """Reverse assertion: only widening is floored. A profile that DENIES a tool
    outright must keep denying it."""
    merged = _merge_permissions(
        policy.agent_permissions("code"), {"_all": {"tools": {"edit": "deny"}}}, "code")
    assert merged["edit"] == "deny"


def test_a_profile_may_still_widen_a_named_shell_path():
    """Reverse assertion, the other side of the line: an explicit path pattern
    is a deliberate, named act — that is how the bundled `relaxed` profile
    re-allows curl — so it is honoured, not floored."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"curl *": "allow"}}}, "review")
    assert _resolve(merged["bash"], "curl https://example.com") == "allow"


def test_a_profile_leaving_file_tools_alone_changes_nothing_about_them():
    before = policy.agent_permissions("explore")
    merged = _merge_permissions(
        before, {"_all": {"tools": {"webfetch": "allow"}}}, "explore")
    assert merged["webfetch"] == "allow"
    for tool in ("read", "edit", "grep"):
        assert merged[tool] == before[tool]


def test_the_documented_profile_example_is_the_shape_this_pins():
    """If the README stops showing `tools: edit: allow` the tests above still
    pass but no longer pin the documented path. Read the real file."""
    readme = (policy.PERMISSIONS_DIR / "README.md").read_text(encoding="utf-8")
    normalised = " ".join(readme.split())
    assert "tools: edit: allow" in normalised, (
        "the README no longer documents widening a file tool; "
        "re-check what the merge has to floor")


# ----------------------------------------------------------------------
# A profile's shell rules are APPENDED, so they land after the two
# ordering-sensitive denies at the end of the base map.
#
# `*>*` is documented at the top of policy.py as necessarily LAST — it is the
# only thing stopping a read-only worker from writing via redirect. A profile
# adding a pattern the base does not already have pushed it out of last place:
# with `"jq *": allow`, `jq . data.json > overwrite.json` resolved to ALLOW in
# review mode. The shipped `relaxed` profile does NOT trip this (its keys exist
# in the base already, and dict assignment keeps an existing key's position),
# which is exactly why reading the shipped profile was not enough to see it.
# ----------------------------------------------------------------------

NEW_PATTERN_PROFILE = {"_all": {"shell": {"jq *": "allow", "httpie *": "allow"}}}


@pytest.mark.parametrize("mode", ["review", "explore", "test"])
def test_a_profile_cannot_push_the_redirect_deny_off_the_end(mode):
    merged = _merge_permissions(
        policy.agent_permissions(mode), NEW_PATTERN_PROFILE, mode)
    assert list(merged["bash"])[-1] == "*>*", (
        f"{mode}: the redirect deny is no longer last")
    assert _resolve(merged["bash"], "jq . data.json > overwrite.json") == "deny"
    assert _resolve(merged["bash"], "httpie x > steal.txt") == "deny"
    # ...and the widening the profile actually asked for still works.
    assert _resolve(merged["bash"], "jq . data.json") == "allow"


@pytest.mark.parametrize("mode", ["review", "explore", "test", "code"])
def test_a_profile_cannot_push_the_path_denies_off_the_end(mode):
    merged = _merge_permissions(
        policy.agent_permissions(mode), NEW_PATTERN_PROFILE, mode)
    assert _resolve(merged["bash"], "jq . .env") == "deny", mode
    assert _resolve(merged["bash"], "jq . config/auth.json") == "deny", mode


def test_the_shipped_relaxed_profile_keeps_the_redirect_deny_last():
    """Pin the case that made this invisible: `relaxed` only touches patterns
    the base already has, so nothing moved and reading it proved nothing."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        load_permission_profile("relaxed"), "review")
    assert list(merged["bash"])[-1] == "*>*"
    assert _resolve(merged["bash"], "curl https://x.invalid") == "allow"
    assert _resolve(merged["bash"], "curl https://x.invalid > out") == "deny"


def test_a_profile_may_still_deliberately_re_allow_a_named_path():
    """The line stays where it was: naming the pattern is a deliberate act and
    wins; adding an unrelated command does not silently void the floor."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"*.env*": "allow"}}}, "review")
    assert _resolve(merged["bash"], "cat .env") == "allow"


def test_a_profile_may_still_deliberately_re_allow_redirects():
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"*>*": "allow"}}}, "review")
    assert _resolve(merged["bash"], "cat f > out") == "allow"


def test_a_profile_can_shut_the_shell_off_for_a_mode():
    """`tools: {bash: deny}` was silently inert — the merged rule map overwrote
    it, so an operator who thought they had disabled the shell had not."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"tools": {"bash": "deny"}}}, "review")
    assert merged["bash"] == "deny"


def test_an_ordinary_profile_still_gets_the_merged_shell_map():
    """Reverse assertion: an override of EITHER shape is honoured, and a
    profile that does not name `bash` still gets the merged map."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"jq *": "allow"}}}, "review")
    assert isinstance(merged["bash"], dict)
    assert _resolve(merged["bash"], "jq . x.json") == "allow"


def test_a_profile_agreeing_with_a_deny_does_not_pin_it_in_place():
    """Skipping every pattern the profile MENTIONED was too broad.

    A profile that names `*.env*: deny` is agreeing with the floor, not
    overriding it — but the exemption treated agreement as an override, left the
    deny at its base position, and the profile's own new allow then outranked it.
    Judged on the effective value now: only a non-deny value is an override.
    """
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"*.env*": "deny", "jq *": "allow"}}}, "review")
    assert _resolve(merged["bash"], "jq . .env") == "deny"
    assert _resolve(merged["bash"], "jq . data.json") == "allow"


def test_a_profile_widening_a_deny_is_still_an_override():
    """Reverse assertion: the narrowing must not break the deliberate case."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"*.env*": "allow", "jq *": "allow"}}}, "review")
    assert _resolve(merged["bash"], "cat .env") == "allow"


@pytest.mark.parametrize("value", ["deny", "allow", {"*": "deny"},
                                   {"pytest*": "allow", "*": "deny"}])
def test_a_profile_bash_override_is_honoured_whatever_its_shape(value):
    """The first version tested `isinstance(..., str)` and silently dropped a
    dict — which is the MORE natural mistake, since it mirrors OpenCode's own
    config shape."""
    merged = _merge_permissions(
        policy.agent_permissions("review"), {"_all": {"tools": {"bash": value}}},
        "review")
    assert merged["bash"] == value


def test_an_untouched_bash_still_gets_the_merged_map():
    before = policy.agent_permissions("review")
    merged = _merge_permissions(
        before, {"_all": {"shell": {"jq *": "allow"}}}, "review")
    assert isinstance(merged["bash"], dict)
    assert list(merged["bash"])[-1] == "*>*"


@pytest.mark.parametrize("command", [
    "curl https://x.invalid", "sudo rm -rf /", "git push origin main",
    "ssh host", "wget https://x.invalid", "gh pr create",
])
def test_a_profile_adding_an_unrelated_allow_cannot_reach_a_command_deny(command):
    """Round 14 (glm) claimed the other 19 denies also need re-appending. They
    do not, and the reason is the distinction already written at the top of the
    module: those denies are anchored to a command name, so only a pattern that
    matches the SAME command can shadow them — and writing that pattern is a
    deliberate act. Only the path-shaped denies match anywhere in the command,
    which is why they are the two that move."""
    merged = _merge_permissions(
        policy.agent_permissions("review"),
        {"_all": {"shell": {"httpie *": "allow", "jq *": "allow"}}}, "review")
    assert _resolve(merged["bash"], command) == "deny"


@pytest.mark.parametrize("section,expected", [
    ("allow", "must be a mapping"),
    (["shell"], "must be a mapping"),
    ({"shell": "allow"}, "shell' must be a mapping"),
    ({"tools": "deny"}, "tools' must be a mapping"),
    ({"shel": {"curl *": "allow"}}, "unknown keys"),
])
def test_a_malformed_profile_section_is_named_not_ignored(
        tmp_path, monkeypatch, section, expected):
    """`code: allow` was skipped in silence by the merge and
    `code: {shell: allow}` raised AttributeError from inside it — a traceback for
    a typo in the operator's own config file, and a silent no-op for the other
    shape. Both now fail at load, naming the file and the field."""
    import yaml as _yaml

    monkeypatch.setattr(policy, "PERMISSIONS_DIR", tmp_path)
    (tmp_path / "p.yaml").write_text(
        _yaml.safe_dump({"code": section}), encoding="utf-8")
    with pytest.raises(RuntimeError, match=expected):
        load_permission_profile("p")


def test_a_well_formed_profile_still_loads(tmp_path, monkeypatch):
    """Reverse assertion, covering both fields and the _all section."""
    import yaml as _yaml

    monkeypatch.setattr(policy, "PERMISSIONS_DIR", tmp_path)
    (tmp_path / "p.yaml").write_text(_yaml.safe_dump({
        "_all": {"shell": {"curl *": "allow"}, "tools": {"webfetch": "allow"}},
        "code": {"shell": {"wget *": "allow"}},
    }), encoding="utf-8")
    profile = load_permission_profile("p")
    assert profile["code"]["shell"] == {"wget *": "allow"}


def test_the_bundled_profiles_still_load():
    """The shape check must not reject what ships."""
    for name in ("relaxed", "strict"):
        assert load_permission_profile(name)
