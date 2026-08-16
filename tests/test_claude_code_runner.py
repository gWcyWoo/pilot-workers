"""Claude Code runner adapter: config, argv, environment, permissions, events.

Every assertion here corresponds to a failure mode established by probing the
real engine (Claude Code 2.1.233) — the module docstring of
``runners/claude_code_runner`` records which probe produced which rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from pilot_workers.providers import Provider
from pilot_workers.runners import get_runner
from pilot_workers.runners.claude_code_runner import (
    ClaudeCodeRunner,
    translate_permissions,
)


@pytest.fixture
def provider() -> Provider:
    return Provider(
        key="glm-cc",
        provider_id="glm-anthropic",
        model_id="glm-5.3",
        base_url="https://open.bigmodel.cn/api/anthropic",
        display_name="GLM 5.3 (Claude Code harness)",
        context_tokens=1_000_000,
        output_tokens=128_000,
        runner="claude-code",
    )


@pytest.fixture
def runner() -> ClaudeCodeRunner:
    # A fresh instance per test: parse_events carries the assistant-message
    # dedupe state, and the registry singleton would leak it between tests.
    return ClaudeCodeRunner()


def _argv(runner: ClaudeCodeRunner, provider: Provider, mode: str, tmp_path):
    config = runner.build_config(provider, mode)
    return runner.build_command(
        tmp_path / "claude", provider, mode, tmp_path, "run-1", None,
        config=config), config


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------


def test_the_runner_is_registered_under_its_name():
    assert get_runner("claude-code").name == "claude-code"


# ----------------------------------------------------------------------
# argv
# ----------------------------------------------------------------------


def test_isolation_flags_are_always_on_the_command_line(runner, provider, tmp_path):
    """--bare and --strict-mcp-config are the isolation guarantee, not options.

    Without --bare a `-p` session runs the project's hooks and MCP servers
    untrusted and may fall back to the operator's claude.ai login; without
    --strict-mcp-config an MCP config discovered elsewhere would still load.
    """
    argv, _ = _argv(runner, provider, "explore", tmp_path)
    assert "--bare" in argv
    assert "-p" in argv
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_the_worker_runs_inside_the_workdir(runner, provider, tmp_path):
    """This engine has no --dir: it takes its workspace from process.cwd().

    Left at pw9's dispatch cwd the worker sees an empty project and — because
    that cwd is under $PILOT_WORKERS_HOME — every read is refused by the
    credential-path deny. The first end-to-end run failed exactly this way.
    """
    assert runner.working_directory(tmp_path) == tmp_path
    argv, _ = _argv(runner, provider, "explore", tmp_path)
    assert "--add-dir" not in argv


def test_the_opencode_runner_still_keeps_the_dispatch_cwd(tmp_path):
    """It is told where to work with --dir; changing its cwd is not this
    change's business."""
    assert get_runner("opencode").working_directory(tmp_path) is None


def test_the_model_id_and_output_format_reach_the_engine(runner, provider, tmp_path):
    argv, _ = _argv(runner, provider, "explore", tmp_path)
    assert argv[argv.index("--model") + 1] == "glm-5.3"
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_autocompact_is_pinned_from_the_provider_not_the_engines_guess(
    runner, provider, tmp_path
):
    """The engine invents a 200k window for a model it does not recognise.

    Left alone it would compact a 1M-token model's context at 200k.
    """
    argv, _ = _argv(runner, provider, "explore", tmp_path)
    assert argv[argv.index("--autocompact") + 1] == "1000000"


def test_autocompact_clamps_to_the_range_the_flag_accepts(runner, tmp_path):
    small = Provider(
        key="tiny", provider_id="t", model_id="m", base_url="https://e",
        display_name="T", context_tokens=8_000, output_tokens=1_000,
        runner="claude-code")
    argv, _ = _argv(runner, small, "explore", tmp_path)
    assert argv[argv.index("--autocompact") + 1] == "100000"


def test_effort_is_forwarded_only_when_the_provider_sets_one(runner, provider, tmp_path):
    argv, _ = _argv(runner, provider, "explore", tmp_path)
    assert "--effort" not in argv

    reasoning = Provider(**{**provider.__dict__, "effort": "high"})
    argv, config = _argv(runner, reasoning, "explore", tmp_path)
    assert argv[argv.index("--effort") + 1] == "high"


def test_build_command_refuses_to_run_without_the_config_built_for_this_run(
    runner, provider, tmp_path
):
    """Rebuilding it here would silently drop the run's --permissions profile."""
    with pytest.raises(RuntimeError, match="without it"):
        runner.build_command(
            tmp_path / "claude", provider, "explore", tmp_path, "run-1", None)


def test_a_session_resumes_through_the_engines_own_flag(runner, provider, tmp_path):
    config = runner.build_config(provider, "resume")
    argv = runner.build_command(
        tmp_path / "claude", provider, "resume", tmp_path, "run-2", "sess-abc",
        config=config)
    assert argv[argv.index("--resume") + 1] == "sess-abc"


# ----------------------------------------------------------------------
# environment — the subscription-isolation guarantee
# ----------------------------------------------------------------------


def test_the_engine_profile_is_relocated_into_the_run_sandbox(
    runner, provider, tmp_path
):
    """CLAUDE_CONFIG_DIR is what keeps the operator's own ~/.claude untouched."""
    paths = {name: tmp_path / name for name in
             ("root", "config", "data", "state", "cache")}
    env = runner.runner_environment(
        provider, runner.build_config(provider, "explore"), paths=paths)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "config" / "claude")
    assert ".claude" not in env["CLAUDE_CONFIG_DIR"].replace(
        str(tmp_path / "config" / "claude"), "")


def test_every_phone_home_switch_is_set_in_the_child(runner, provider, tmp_path):
    """Documented opt-outs, and set here only — never in the operator's shell."""
    env = runner.runner_environment(
        provider, runner.build_config(provider, "explore"))
    for key in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY",
                "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER", "DO_NOT_TRACK"):
        assert env[key] == "1", key


def test_the_credential_variable_is_always_present_so_a_login_cannot_take_over(
    runner, provider
):
    """A base URL with no credential variable leaves a saved claude.ai login
    as the active credential — which would send that OAuth token to the
    third-party endpoint. The variable must exist even before it is filled."""
    env = runner.runner_environment(
        provider, runner.build_config(provider, "explore"))
    assert "ANTHROPIC_AUTH_TOKEN" in env
    assert env["ANTHROPIC_BASE_URL"] == provider.base_url


def test_apply_credential_fills_the_token(runner, provider):
    env = runner.runner_environment(
        provider, runner.build_config(provider, "explore"))
    runner.apply_credential(env, "sk-secret-value")
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret-value"
    assert env["ANTHROPIC_API_KEY"] == "sk-secret-value"


def test_the_credential_never_reaches_argv(runner, provider, tmp_path):
    argv, _ = _argv(runner, provider, "code", tmp_path)
    assert not any("sk-" in part for part in argv)


# ----------------------------------------------------------------------
# permission translation
# ----------------------------------------------------------------------


def test_code_mode_allows_the_shell_and_keeps_the_guardrail_denies():
    from pilot_workers import policy

    allow, deny, _ = translate_permissions(policy.agent_permissions("code"), "code")
    assert "Bash" in allow
    assert "Bash(git push*)" in deny
    assert "Bash(sudo *)" in deny


def test_a_read_only_mode_never_emits_a_bare_bash_rule():
    """Neither direction: a bare `Bash` allow would open the shell, and a bare
    `Bash` deny removes the tool from the model's context entirely — taking the
    mode's own allowed read commands with it. `dontAsk` denies the rest."""
    from pilot_workers import policy

    allow, deny, _ = translate_permissions(
        policy.agent_permissions("explore"), "explore")
    assert "Bash" not in allow
    assert "Bash" not in deny
    assert "Bash(git diff*)" in allow


def test_read_only_modes_lose_the_write_tools_entirely():
    from pilot_workers import policy

    _, deny, _ = translate_permissions(policy.agent_permissions("review"), "review")
    for tool in ("Edit", "Write", "NotebookEdit"):
        assert tool in deny


def test_the_credential_path_floor_holds_in_every_mode():
    """Expressed as Read/Edit rules because this engine's Bash grammar has no
    leading wildcard — and those rules also cover `cat`/`head`/`tail`/`sed`."""
    from pilot_workers import policy

    for mode in ("code", "explore", "review", "test", "discuss"):
        _, deny, _ = translate_permissions(policy.agent_permissions(mode), mode)
        assert "Read(**/.env)" in deny, mode
        assert "Read(**/auth.json)" in deny, mode
        # This tool's own credential store, and the Codex CLI's home that it
        # used to live inside.
        assert "Read(~/.pilot-workers/**)" in deny, mode
        assert "Read(~/.codex/**)" in deny, mode
        assert "Write(**/.env)" in deny, mode


def test_subagents_and_network_tools_are_denied_in_every_mode():
    from pilot_workers import policy

    for mode in ("code", "explore", "review", "test", "discuss"):
        _, deny, _ = translate_permissions(policy.agent_permissions(mode), mode)
        for tool in ("WebFetch", "WebSearch", "Task", "Agent"):
            assert tool in deny, (mode, tool)


def test_a_dry_run_shows_what_the_translation_could_not_carry(tmp_path):
    """The claim is made in CLAUDE.md and in translate_permissions' docstring,
    so it has to be true of the command an operator actually runs."""
    from pilot_workers.cli import run as run_mod

    summary = run_mod.dry_run_summary(
        providers_module().PROVIDERS["glm-cc"], "explore", tmp_path)
    reported = summary["unmappable_permissions"]
    assert any("*>*" in item for item in reported)


def test_a_runner_with_a_clean_translation_reports_nothing(tmp_path):
    """An empty list would read as a reassurance the key was even checked."""
    from pilot_workers.cli import run as run_mod

    summary = run_mod.dry_run_summary(
        providers_module().PROVIDERS["glm"], "explore", tmp_path)
    assert "unmappable_permissions" not in summary


def providers_module():
    from pilot_workers import providers

    return providers


def test_patterns_this_engine_cannot_express_are_reported_not_dropped():
    """`*auth.json*` and `*>*` have no Bash grammar here. Silently discarding
    them would make a dry run claim a policy it is not enforcing."""
    from pilot_workers import policy

    _, _, unmappable = translate_permissions(
        policy.agent_permissions("explore"), "explore")
    assert any("*>*" in item for item in unmappable)
    assert any("*auth.json*" in item for item in unmappable)


def test_an_allow_shadowed_by_a_broader_deny_is_reported_and_not_emitted():
    """pw9 resolves last-match-wins, so `test` mode denies `python*` and then
    re-allows `python -m pytest*`. This engine resolves deny BEFORE allow, so
    the override cannot survive; the strict reading is kept and the loss is
    named."""
    from pilot_workers import policy

    allow, deny, unmappable = translate_permissions(
        policy.agent_permissions("test"), "test")
    assert "Bash(python*)" in deny
    assert "Bash(python -m pytest*)" not in allow
    assert any("python -m pytest*" in item and "shadowed" in item
               for item in unmappable)


def test_an_unshadowed_test_runner_allow_still_gets_through():
    from pilot_workers import policy

    allow, _, _ = translate_permissions(policy.agent_permissions("test"), "test")
    assert "Bash(pytest*)" in allow


def test_the_settings_blob_carries_the_rules_and_closes_the_webfetch_preflight(
    runner, provider, tmp_path
):
    """The preflight sends the requested hostname to api.anthropic.com and is
    explicitly NOT covered by CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC."""
    argv, config = _argv(runner, provider, "explore", tmp_path)
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["skipWebFetchPreflight"] is True
    assert settings["permissions"]["allow"] == config["settings"]["permissions"]["allow"]
    assert "Read(**/.env)" in settings["permissions"]["deny"]


def test_read_only_modes_run_under_dont_ask(runner, provider):
    assert runner.build_config(provider, "explore")["permission_mode"] == "dontAsk"
    assert runner.build_config(provider, "code")["permission_mode"] == "acceptEdits"


def test_no_mode_offers_a_tool_its_permissions_deny(runner, provider):
    for mode in ("explore", "review", "discuss", "test"):
        tools = runner.build_config(provider, mode)["tools"]
        assert "Write" not in tools, mode
        assert "Edit" not in tools, mode
        assert "Task" not in tools, mode
        assert "WebFetch" not in tools, mode


# ----------------------------------------------------------------------
# event translation
# ----------------------------------------------------------------------


def _assistant(message_id: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-08-15T14:29:56.040Z",
        "session_id": "sess-1",
        "message": {"id": message_id, "role": "assistant", "content": blocks,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    }


def test_one_api_round_trip_counts_as_one_step_however_many_lines_it_spans(runner):
    """The engine splits a single assistant turn across several stream-json
    lines that share a message.id — a thinking block, then each tool_use.
    Counting lines would triple-count the run against its step cap."""
    lines = [
        _assistant("msg_a", [{"type": "thinking", "thinking": "hm"}]),
        _assistant("msg_a", [{"type": "tool_use", "name": "Read",
                              "input": {"file_path": "/tmp/x"}}]),
        _assistant("msg_a", [{"type": "tool_use", "name": "Bash",
                              "input": {"command": "ls"}}]),
        _assistant("msg_b", [{"type": "text", "text": "done"}]),
    ]
    steps = sum(
        1 for line in lines for ev in runner.parse_events(line) if ev.kind == "step")
    assert steps == 2


def test_thinking_token_ticks_are_dropped(runner):
    """Eighty-plus of these arrive in a fifteen-second run; each carries no
    information and would flood the rendered log."""
    events = runner.parse_events({
        "type": "system", "subtype": "thinking_tokens",
        "estimated_tokens": 12, "session_id": "sess-1"})
    assert [ev.kind for ev in events] == ["session"]


def test_the_session_id_is_picked_up_so_a_run_can_be_resumed(runner):
    events = runner.parse_events({
        "type": "system", "subtype": "init", "session_id": "sess-xyz",
        "model": "glm-5.3"})
    assert [(ev.kind, ev.session_id) for ev in events] == [("session", "sess-xyz")]


def test_the_final_report_comes_from_the_result_message(runner):
    """dispatch.parse_jsonl keeps the LAST text event, and the engine's own
    assembled `result` is the report — not whichever block streamed last."""
    events = runner.parse_events({
        "type": "result", "subtype": "success", "session_id": "s",
        "result": "the report", "usage": {}})
    texts = [ev.text for ev in events if ev.kind == "text"]
    assert texts == ["the report"]


def test_usage_is_taken_only_from_the_result_message(runner):
    """Every streamed assistant `usage` is zero; the totals arrive once."""
    (step,) = [ev for ev in runner.parse_events({
        "type": "result", "subtype": "success", "result": "x",
        "usage": {"input_tokens": 1557, "output_tokens": 262,
                  "cache_read_input_tokens": 960,
                  "cache_creation_input_tokens": 12,
                  "output_tokens_details": {"thinking_tokens": 40}},
    }) if ev.kind == "step"]
    assert step.tokens is not None
    assert (step.tokens.input, step.tokens.output) == (1557, 262)
    assert (step.tokens.cache_read, step.tokens.cache_write) == (960, 12)
    assert step.tokens.reasoning == 40


def test_a_failed_run_emits_an_error_event(runner):
    kinds = [ev.kind for ev in runner.parse_events({
        "type": "result", "subtype": "error_during_execution",
        "is_error": True, "result": "", "usage": {}})]
    assert "error" in kinds


def test_a_permission_denial_is_classified_as_such_not_as_a_tool_failure(runner):
    """Verified engine wording: "Permission to use Bash with command ... has
    been denied." The verdict counts these separately from real tool errors."""
    (event,) = [ev for ev in runner.parse_events({
        "type": "user", "timestamp": "2026-08-15T14:29:56.040Z",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "is_error": True,
            "content": "Permission to use Bash with command rm -rf /tmp/nope "
                       "has been denied."}]},
    }) if ev.kind == "tool"]
    assert event.tool is not None
    assert event.tool.status == "error"
    assert event.tool.is_permission_denied is True


def test_an_ordinary_tool_failure_is_not_counted_as_a_denial(runner):
    (event,) = [ev for ev in runner.parse_events({
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "is_error": True,
            "content": "ENOENT: no such file"}]},
    }) if ev.kind == "tool"]
    assert event.tool is not None
    assert event.tool.is_permission_denied is False


def test_the_system_permission_denied_event_is_not_double_counted(runner):
    """The engine reports a denial twice — once as system/permission_denied and
    once as the is_error tool_result. Only the tool_result is translated."""
    events = runner.parse_events({
        "type": "system", "subtype": "permission_denied", "session_id": "s",
        "tool_name": "Bash", "message": "Permission to use Bash ... denied."})
    assert not [ev for ev in events if ev.kind == "tool"]


def test_timestamps_become_epoch_milliseconds(runner):
    """The engine stamps ISO-8601; UnifiedEvent.ts is epoch ms, because that is
    what dispatch.parse_jsonl subtracts to report a run's duration."""
    expected = int(
        datetime(2026, 8, 15, 14, 29, 56, 40_000, tzinfo=timezone.utc).timestamp()
        * 1000)
    (event,) = [ev for ev in runner.parse_events(
        _assistant("m", [{"type": "text", "text": "hi"}])) if ev.kind == "text"]
    assert event.ts == expected


def test_a_missing_or_unparseable_timestamp_is_not_fatal(runner):
    (event,) = [ev for ev in runner.parse_events({
        "type": "result", "subtype": "success", "result": "x", "usage": {},
        "timestamp": "not-a-date"}) if ev.kind == "text"]
    assert event.ts is None


# ----------------------------------------------------------------------
# credentials
# ----------------------------------------------------------------------


def test_an_oauth_credential_is_refused(runner, provider):
    """An oauth token here would be the operator's own Claude subscription —
    exactly what --bare exists to keep out of a worker."""
    with pytest.raises(RuntimeError, match="API auth"):
        runner.parse_credential(
            provider, {"glm-anthropic": {"type": "oauth", "access": "tok"}})


def test_an_api_credential_round_trips(runner, provider):
    payload = runner.credential_payload(provider, "sk-key-value")
    assert runner.parse_credential(provider, payload) == "sk-key-value"


def test_pw9_does_not_manage_the_engines_runtime(runner):
    """Claude Code installs and updates itself; `uninstall runner` must never
    remove the operator's own binary."""
    assert runner.runtime_root() is None
    assert runner.install_script() is None
    assert runner.pinned_version is None
