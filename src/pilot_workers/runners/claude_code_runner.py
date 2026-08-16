"""Claude Code runner adapter — concrete Runner for the `claude` CLI.

Claude Code is used strictly as a HARNESS: the agent loop, tool set and
permission engine are its, the model is whatever the provider YAML points
`base_url` at (GLM, DeepSeek, ...) through its Anthropic-compatible endpoint.
Nothing here assumes an Anthropic model or an Anthropic account.

Verified against Claude Code 2.1.233 on 2026-08-15, driving glm-5.3 through
https://open.bigmodel.cn/api/anthropic.

Four things about this engine are counter-intuitive and each cost a probe to
establish. They are the reason this file looks the way it does.

**1. There is no engine-side turn cap.** The agent schema accepts `maxTurns`
("Maximum conversation turns before the agent stops") and the engine can emit
`subtype: "error_max_turns"` — but only for SUBAGENTS. An inline `--agents`
definition selected with `--agent` is NOT capped on the main `-p` loop:
`maxTurns: 1` still ran four turns to a clean `end_turn`. `Runner` therefore
cannot honour "build_config must hard-stop the engine at its step cap" here;
`runtime.run_process` enforces the cap on the read side instead, by
terminating the child once `max_steps` step events have gone by. See
`runners/base.py`, which documents both halves of that contract.

**2. `-p` is not isolated by default.** A print-mode session shows no
workspace-trust dialog, so it executes the hooks in a project's
`.claude/settings.json` and connects the servers in its `.mcp.json` without
asking. `--bare` is what turns that off, and it is not optional here: it also
stops the engine reading OAuth credentials or the system keychain, which is
what keeps a dispatch off the operator's own Claude subscription. Belt and
braces: `CLAUDE_CONFIG_DIR` points at the per-run sandbox, so `~/.claude` is
neither read nor written (verified: a full dispatch left it byte-identical).

**3. Setting `ANTHROPIC_BASE_URL` alone is a credential leak.** Per Anthropic's
gateway documentation, a base URL with no credential variable leaves a saved
claude.ai login as the active credential — which would send the operator's
OAuth token to the third-party endpoint. `runner_environment` therefore always
exports `ANTHROPIC_AUTH_TOKEN`; it is never left to fall back.

**4. Model metadata is guessed for any model the engine does not know.** A
`[claude-code:unrecognized_model]` line on stderr is a warning, not an error
(deduped once per model per session), but everything downstream of the lookup
is a default: `contextWindow: 200000`, `maxOutputTokens: 32000`,
`provider: "firstParty"`, and a `total_cost_usd` computed from the wrong price
table. `--autocompact` is therefore pinned from the provider YAML, and no cost
figure from this engine is ever propagated — `parse_events` reports tokens only.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
from typing import Any

from pilot_workers import policy
from pilot_workers.providers import Provider, profile_root
from pilot_workers.runners.base import (
    Runner,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
)

# The engine's own wording when a permission rule refuses a call; it arrives as
# the text of an `is_error` tool_result. Verified 2026-08-15: a `Bash(rm *)`
# deny produced "Permission to use Bash with command rm -rf /tmp/nope has been
# denied." There is also a `system/permission_denied` event carrying the same
# refusal — deliberately ignored, or every denial would be counted twice.
_PERMISSION_DENIED_MARK = "Permission to use "

# Engine event noise: one line per thinking-token tick, eighty-plus of them in a
# fifteen-second run. Translating them would flood the rendered log and the
# JSONL aggregation with events that carry no information.
_IGNORED_SYSTEM_SUBTYPES = frozenset({"thinking_tokens", "stream_event"})

# Tools whose output is a file dump or a plain confirmation; the renderer shows
# the input brief and suppresses the output line. Same intent as the OpenCode
# adapter's list, spelled in this engine's tool names.
SILENT_OUTPUT_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "TodoWrite"})

_INPUT_LIMIT = 200

# Built-in tools each mode may use. `--tools` REPLACES the default set, so this
# is the whole surface: anything absent here cannot be called at all, which is
# a stronger guarantee than a deny rule and the reason `Task` never appears
# (a subagent would run outside this run's step accounting and permissions).
_TOOLS_BY_MODE = {
    "code": ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"),
    "resume": ("Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"),
    "explore": ("Read", "Glob", "Grep", "Bash", "TodoWrite"),
    "review": ("Read", "Glob", "Grep", "Bash", "TodoWrite"),
    "test": ("Read", "Glob", "Grep", "Bash", "TodoWrite"),
    "discuss": ("Read", "Glob", "Grep", "Bash", "TodoWrite"),
}

# Paths no worker may read or write in any mode, as gitignore patterns (the
# syntax Claude Code's Read/Edit rules use). This is the same floor
# `policy.file_tool_path_rules` states for the OpenCode adapter, re-expressed —
# plus this tool's own credential store and the operator's Claude profile,
# which a worker driven by a third-party model has no business reading.
#
# Worth knowing: the engine applies Read/Edit deny rules to the file COMMANDS
# it recognises in Bash too (`cat`, `head`, `tail`, `sed`), so this floor
# covers `cat .env` as well as the Read tool. It does not cover a subprocess
# that opens the file itself (a python one-liner) — neither does OpenCode's.
_PROTECTED_READ_PATTERNS = (
    "**/.env",
    "**/.env.*",
    "**/*.env",
    "**/auth.json",
    "**/*auth.json*",
    # This tool's own store: provider credentials, run sandboxes and their
    # symlinked keys all live here.
    "~/.pilot-workers/**",
    # The Codex CLI's home, which pw9's data used to sit inside. Still listed
    # because it holds Codex's own auth.json and sessions, and a worker driven
    # by a third-party model has no business reading either.
    "~/.codex/**",
    "~/.claude/**",
    "~/.aws/**",
    "~/.ssh/**",
)

# `--autocompact` accepts "auto" or a token count the engine clamps to this
# range. The provider's real context window is the right number; without it the
# engine compacts against the 200k it guesses for an unknown model.
_AUTOCOMPACT_MIN = 100_000
_AUTOCOMPACT_MAX = 1_000_000


class ClaudeCodeRunner(Runner):
    """Adapter for the `claude` CLI in headless (`-p`) mode."""

    name = "claude-code"

    def __init__(self) -> None:
        # Per-run stream state. The engine splits ONE assistant turn into
        # several stream-json events that share a `message.id` (a thinking
        # block, then each tool_use, as separate lines), so counting events
        # would count one API round-trip three times. Deduping by message id is
        # the only way to recover the real step count, and it is the one piece
        # of state `parse_events` needs. Reset in `build_command`, which runs
        # once per dispatch before any event is read; the post-mortem reparse
        # path builds a fresh runner in its own process.
        self._last_assistant_id: str | None = None

    # ------------------------------------------------------------------
    # config / command / env
    # ------------------------------------------------------------------

    def build_config(
        self, provider: Provider, mode: str, permission_profile: str | None = None,
    ) -> dict:
        """Everything this engine needs, as data.

        Returns the two blobs that reach the CLI (`settings` for `--settings`,
        `system_prompt` for `--system-prompt`) alongside the scalar flags, plus
        the `share` / `enabled_providers` keys `cli/run.dry_run_summary` reads
        from every runner's config.
        """
        permissions = policy.effective_permissions(
            provider, mode, permission_profile=permission_profile)
        allow, deny, unmappable = translate_permissions(permissions, mode)
        return {
            # Consumed by dry_run_summary, which is runner-neutral.
            "share": "disabled",
            "enabled_providers": [provider.provider_id],
            "settings": {
                "permissions": {"allow": allow, "deny": deny},
                # The WebFetch preflight sends the requested hostname to
                # api.anthropic.com and is explicitly NOT covered by
                # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC. WebFetch is not in
                # any mode's tool set, so this only closes the door twice — but
                # it closes the one egress the telemetry switches cannot.
                "skipWebFetchPreflight": True,
                "includeCoAuthoredBy": False,
            },
            "system_prompt": policy.load_prompt(mode),
            "tools": list(_TOOLS_BY_MODE[mode]),
            # `code` may write without a prompt; every other mode auto-denies
            # anything the allow list does not name. `dontAsk` is what makes an
            # allow-list-only policy safe: there is no interactive fallback in
            # `-p` anyway, and without it an unlisted call would hang or pass.
            "permission_mode": "acceptEdits" if mode in ("code", "resume") else "dontAsk",
            "autocompact": _autocompact_tokens(provider),
            # Reported so a dry run shows what could not be carried across
            # rather than hiding it (see translate_permissions).
            "unmappable_permissions": unmappable,
        }

    def build_command(
        self, binary: Path, provider: Provider, mode: str,
        workdir: Path, run_id: str, session: str | None,
        config: dict | None = None,
    ) -> list[str]:
        # One dispatch, one stream: clear the assistant-id dedupe before any
        # event of this run is translated.
        self._last_assistant_id = None
        if config is None:
            # Never silently rebuild: the caller's config may carry a
            # `--permissions` profile this method cannot see, and dispatching
            # with quietly different rules than the operator asked for is the
            # failure mode worth being loud about.
            raise RuntimeError(
                "claude-code runner requires the config built for this run; "
                "build_command was called without it")
        command = [
            str(binary),
            # Not optional. Without it a `-p` session runs the project's hooks
            # and MCP servers untrusted, and may fall back to the operator's
            # claude.ai login. See this module's docstring, point 2.
            "--bare",
            "-p",
            "--model", provider.model_id,
            "--system-prompt", config["system_prompt"],
            "--tools", ",".join(config["tools"]),
            "--permission-mode", config["permission_mode"],
            "--settings", json.dumps(config["settings"], separators=(",", ":")),
            # No --mcp-config anywhere, so this pins the server set to empty
            # rather than merely preferring ours.
            "--strict-mcp-config",
            "--autocompact", str(config["autocompact"]),
            "--output-format", "stream-json",
            "--verbose",
        ]
        # No --add-dir: `working_directory` below puts the process IN the
        # workdir, so it is already the session's working directory. Adding it
        # again would only widen the tool-access set for no gain.
        if provider.effort:
            command.extend(["--effort", provider.effort])
        if session:
            command.extend(["--resume", session])
        return command

    def runner_environment(
        self, provider: Provider, config: dict,
        paths: dict[str, Path] | None = None,
    ) -> dict[str, str]:
        # Neutral keys (SAFE_ENV_KEYS, XDG_*, NO_COLOR, CI) are added by the
        # runtime layer, not here.
        if paths is None:
            from pilot_workers.providers import profile_paths

            paths = profile_paths(provider)
        env = {
            # The whole Claude Code profile — settings, session transcripts,
            # project state, backups — relocates here. This is what keeps the
            # operator's own ~/.claude untouched by a dispatch.
            "CLAUDE_CONFIG_DIR": str(paths["config"] / "claude"),
            # Set unconditionally: a base URL with no credential variable
            # leaves a saved claude.ai login as the active credential (see
            # this module's docstring, point 3). runner_environment cannot read
            # the credential file, so run.py's `secret` is not available here —
            # the value is filled in by `apply_credential` below, which the
            # runtime calls with the key it already read.
            "ANTHROPIC_AUTH_TOKEN": "",
            # Belt and braces with the token: if anything ever cleared the
            # variable above, an empty API key still cannot become an OAuth
            # session.
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
            # Opt-outs, all documented by Anthropic and set ONLY in this child
            # process — the operator's own sessions are untouched. Together
            # they stop usage metrics, error reports, feature-flag fetches,
            # account sync and the surveys.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DO_NOT_TRACK": "1",
            # An autoupdate would replace the very binary the operator's own
            # sessions run; DISABLE_AUTOUPDATER above is the switch, this is
            # the second half of the same intent.
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        }
        if provider.base_url:
            env["ANTHROPIC_BASE_URL"] = provider.base_url
        return env

    def working_directory(self, workdir: Path) -> Path | None:
        # This engine takes its workspace from process.cwd(); there is no
        # `--dir`. Left at pw9's dispatch cwd the worker sees an empty project
        # AND sits inside `$PILOT_WORKERS_HOME`, which the credential-path deny
        # covers — so every read is refused and the run reports nothing.
        return workdir

    def apply_credential(self, env: dict[str, str], secret: str) -> None:
        """Put the provider key where this engine reads it.

        Separate from ``runner_environment`` because that method has no access
        to the credential file; the runtime knows the key already (it reads one
        to build the redaction list) and hands it over here.
        """
        env["ANTHROPIC_AUTH_TOKEN"] = secret
        env["ANTHROPIC_API_KEY"] = secret

    def format_task_input(self, task: str, mode: str) -> str:
        return f'<worker-task mode="{mode}">\n{task}\n</worker-task>'

    # ------------------------------------------------------------------
    # event translation
    # ------------------------------------------------------------------

    def parse_events(self, raw: dict) -> list[UnifiedEvent]:
        """Translate one stream-json line into 0..n UnifiedEvents.

        Shapes verified against Claude Code 2.1.233:

        - ``system/init`` — session_id, model, permissionMode
        - ``system/thinking_tokens`` — pure noise, dropped
        - ``assistant`` — ``message.content`` blocks (thinking / tool_use /
          text); several events share one ``message.id`` per API round-trip,
          and their ``usage`` is all zeros
        - ``user`` — ``tool_result`` blocks with ``is_error``
        - ``result`` — the final text in ``result``, and the ONLY authoritative
          ``usage`` in the whole stream
        """
        events: list[UnifiedEvent] = []
        ts = _epoch_ms(raw.get("timestamp"))

        session_id = raw.get("session_id")
        if isinstance(session_id, str) and session_id:
            events.append(UnifiedEvent(kind="session", ts=ts, session_id=session_id))

        event_type = raw.get("type")

        if event_type == "system":
            # Nothing else in the system stream carries information this layer
            # models; the session id above is the whole point of `init`.
            return events

        if event_type == "assistant":
            message = raw.get("message")
            if not isinstance(message, dict):
                return events
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id != self._last_assistant_id:
                # One step per API round-trip, not per stream line. Tokens are
                # deliberately absent: every streamed `usage` here is zero, and
                # the run's real totals arrive once, on the result message.
                self._last_assistant_id = message_id
                events.append(UnifiedEvent(kind="step", ts=ts))
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    events.append(UnifiedEvent(
                        kind="text", ts=ts, text=_as_text(block.get("text"))))
                elif block_type == "thinking":
                    events.append(UnifiedEvent(
                        kind="reasoning", ts=ts,
                        text=_as_text(block.get("thinking"))))
                elif block_type == "tool_use":
                    events.append(UnifiedEvent(
                        kind="tool", ts=ts, tool=_tool_use_call(block)))
            return events

        if event_type == "user":
            message = raw.get("message")
            if not isinstance(message, dict):
                return events
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    events.append(UnifiedEvent(
                        kind="tool", ts=ts, tool=_tool_result_call(block)))
            return events

        if event_type == "result":
            # The final assistant text as the engine itself assembled it. Emitted
            # as a text event so `dispatch.parse_jsonl`, which keeps the LAST
            # one, extracts the PILOT_RESULT block from the real report rather
            # than from whatever block happened to stream last.
            events.append(UnifiedEvent(
                kind="text", ts=ts, text=_as_text(raw.get("result"))))
            # The one place usage is reported. Carried on a step event because
            # that is the only kind the aggregation reads tokens from; the run's
            # step count is therefore round-trips plus this final one, which is
            # immaterial against caps of 80-120 and keeps both figures honest.
            events.append(UnifiedEvent(
                kind="step", ts=ts, tokens=_result_tokens(raw.get("usage"))))
            if raw.get("is_error") or raw.get("subtype") != "success":
                events.append(UnifiedEvent(kind="error", ts=ts))
            return events

        return events

    # ------------------------------------------------------------------
    # binary / credentials
    # ------------------------------------------------------------------

    def runtime_root(self) -> Path | None:
        # Claude Code installs and updates itself; pw9 neither ships nor
        # manages a copy. `install runner claude-code` therefore reports that
        # there is nothing to install, and uninstall never removes the
        # operator's own binary.
        return None

    def binary_path(self) -> Path | None:
        found = shutil.which("claude")
        return Path(found) if found else None

    def resolve_binary(self) -> Path:
        binary = self.binary_path()
        if binary is None or not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(
                "the `claude` CLI is not on PATH; install Claude Code "
                "(https://claude.com/claude-code) — pw9 does not manage it")
        return binary

    # probe_version is NOT overridden. The base implementation's bounded
    # `--version` subprocess is exactly right here: unlike OpenCode, whose
    # probe costs a ~330ms Node startup and earned a two-layer cache, `claude`
    # is a native binary that answers in milliseconds.

    def sandbox_credential_path(self, paths: dict[str, Path]) -> Path:
        # This engine reads no credential FILE — the key travels in the
        # environment (see apply_credential). The sandbox link is still made so
        # provisioning stays uniform and the key is reachable for inspection.
        return paths["data"] / "claude-code" / "credentials.json"

    def credential_path(self, provider: Provider) -> Path:
        return profile_root(provider) / "data" / "claude-code" / "credentials.json"

    def credential_payload(self, provider: Provider, key: str) -> dict:
        return {provider.provider_id: {"type": "api", "key": key}}

    def parse_credential(self, provider: Provider, payload: dict) -> str:
        entry = payload.get(provider.provider_id)
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"credential file lacks an entry for {provider.provider_id}")
        if entry.get("type") != "api":
            # There is no oauth shape here on purpose: an oauth login would be
            # the operator's own Claude subscription, and driving that through
            # a worker is exactly what `--bare` exists to prevent.
            raise RuntimeError(
                f"credential file lacks API auth for {provider.provider_id}")
        secret = entry.get("key")
        if not isinstance(secret, str) or not secret.strip():
            raise RuntimeError(f"credential is empty for {provider.provider_id}")
        return secret


# ----------------------------------------------------------------------
# permission translation
# ----------------------------------------------------------------------


def translate_permissions(
    permissions: dict[str, Any], mode: str,
) -> tuple[list[str], list[str], list[str]]:
    """Map pw9's permission matrix onto Claude Code's rule model.

    Returns ``(allow, deny, unmappable)``.

    The two models resolve differently and the difference is not cosmetic:

    - pw9/OpenCode is LAST-MATCH-WINS over an ordered map, so a later allow
      deliberately overrides an earlier deny (``test`` mode denies ``python*``
      and then re-allows ``python -m pytest*``).
    - Claude Code evaluates **deny, then ask, then allow**, first match wins.
      A broad deny cannot carry allowlist exceptions.

    So the override idiom cannot survive the crossing. Rather than silently
    dropping either half, this keeps the DENY (the strict reading) and reports
    the shadowed allow in ``unmappable`` — a run that needs it will fail loudly
    on a denied command rather than quietly running something the mode meant to
    forbid.

    Two more pw9 idioms have no expression in this engine's Bash grammar, whose
    specifiers are prefixes (``Bash(git diff *)``) with no leading wildcard:

    - path-shaped denies (``*auth.json*``, ``*.env*``) — carried instead by the
      Read/Edit rules in ``_PROTECTED_READ_PATTERNS``, which this engine also
      applies to the file commands it recognises in Bash
    - the redirect deny (``*>*``) — genuinely not expressible; reported in
      ``unmappable`` so a dry run shows it
    """
    allow: list[str] = []
    deny: list[str] = []
    unmappable: list[str] = []

    bash = permissions.get("bash")
    bash_rules = bash if isinstance(bash, dict) else {}
    # Collected first so the shadowing check below can see every deny, whatever
    # order the source map had them in.
    denied_patterns = {
        pattern for pattern, action in bash_rules.items()
        if action == "deny" and not _is_leading_wildcard(pattern)
    }
    for pattern, action in bash_rules.items():
        if pattern == "*":
            if action == "allow":
                # Bare tool name = every Bash call. The deny direction is
                # deliberately NOT emitted: a bare `Bash` deny removes the tool
                # from the model's context entirely, which would take the
                # mode's allowed read commands with it. `dontAsk` already
                # denies anything unlisted.
                allow.append("Bash")
            continue
        if _is_leading_wildcard(pattern):
            # `*auth.json*`, `*.env*`, `*>*` — no Bash grammar for these.
            unmappable.append(f"bash:{pattern}={action}")
            continue
        if action == "deny":
            deny.append(f"Bash({pattern})")
        elif action == "allow":
            if _shadowed_by_deny(pattern, denied_patterns):
                unmappable.append(
                    f"bash:{pattern}=allow (shadowed by a broader deny; "
                    "this engine resolves deny before allow)")
                continue
            allow.append(f"Bash({pattern})")

    # File tools. The per-path map is replaced wholesale by the protected-path
    # floor: pw9's patterns are globs anchored nowhere, this engine's are
    # gitignore patterns, and re-deriving one from the other per rule would be
    # a translation nobody can verify. The floor states the same intent.
    for pattern in _PROTECTED_READ_PATTERNS:
        deny.append(f"Read({pattern})")
        deny.append(f"Edit({pattern})")
        deny.append(f"Write({pattern})")

    if permissions.get("edit") == "deny":
        # Bare names, so the tools leave the model's context entirely rather
        # than being offered and refused.
        deny.extend(["Edit", "Write", "NotebookEdit"])
    # Never available to a worker in any mode, matching agent_permissions.
    deny.extend(["WebFetch", "WebSearch", "Task", "Agent"])

    return allow, deny, sorted(unmappable)


def _is_leading_wildcard(pattern: str) -> bool:
    return pattern.startswith("*") and pattern != "*"


def _shadowed_by_deny(pattern: str, denied: set[str]) -> bool:
    """Whether a broader deny would beat this allow under deny-first.

    Only prefix shadowing is detected, which is the shape pw9 actually uses
    (``python*`` denied, ``python -m pytest*`` allowed). A deny is broader when
    the allow starts with the deny's literal prefix.
    """
    for denied_pattern in denied:
        prefix = denied_pattern[:-1] if denied_pattern.endswith("*") else denied_pattern
        if prefix and pattern != denied_pattern and pattern.startswith(prefix):
            return True
    return False


def _autocompact_tokens(provider: Provider) -> int:
    """The provider's real context window, clamped to what `--autocompact` takes.

    Without this the engine compacts against the 200k it invents for a model it
    does not recognise, throwing away context a 1M-token model still had room
    for.
    """
    return max(_AUTOCOMPACT_MIN, min(_AUTOCOMPACT_MAX, provider.context_tokens))


# ----------------------------------------------------------------------
# parse_events helpers
# ----------------------------------------------------------------------


def _epoch_ms(value: Any) -> int | None:
    """ISO-8601 (`2026-08-15T14:29:56.040Z`) to epoch milliseconds.

    The engine timestamps messages as ISO strings; `UnifiedEvent.ts` is epoch
    ms because that is what the OpenCode adapter reports and what
    `dispatch.parse_jsonl` subtracts to get a duration.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _result_tokens(usage: Any) -> TokenUsage:
    if not isinstance(usage, dict):
        return TokenUsage()
    details = usage.get("output_tokens_details")
    reasoning = _safe_int(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    return TokenUsage(
        input=_safe_int(usage.get("input_tokens")),
        output=_safe_int(usage.get("output_tokens")),
        reasoning=reasoning,
        cache_read=_safe_int(usage.get("cache_read_input_tokens")),
        cache_write=_safe_int(usage.get("cache_creation_input_tokens")),
    )


def _tool_use_call(block: dict) -> ToolCall:
    name = block.get("name")
    if not isinstance(name, str):
        name = "?"
    tool_input = block.get("input")
    return ToolCall(
        name=name,
        # The call, not its outcome: the result arrives later as a separate
        # tool_result event. "running" keeps it out of the error tallies.
        status="running",
        input_brief=_tool_input_brief(tool_input if isinstance(tool_input, dict) else {}),
        output_brief="",
        error=None,
        is_permission_denied=False,
        silent_output=name in SILENT_OUTPUT_TOOLS,
    )


def _tool_result_call(block: dict) -> ToolCall:
    content = block.get("content")
    text = content if isinstance(content, str) else json.dumps(
        content, ensure_ascii=False) if content is not None else ""
    is_error = bool(block.get("is_error"))
    denied = is_error and _PERMISSION_DENIED_MARK in text
    return ToolCall(
        # The result carries only a tool_use_id, never the tool name, and this
        # translation is per-event by contract — so the name is unknown here.
        # The matching tool_use event above already named it.
        name="?",
        status="error" if is_error else "completed",
        input_brief="",
        output_brief=_first_line(text, _INPUT_LIMIT),
        error=_first_line(text, _INPUT_LIMIT) if is_error else None,
        is_permission_denied=denied,
        silent_output=False,
    )


def _trim(value: Any, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _short_path(value: str) -> str:
    """~ for home, and keep only the last 3 segments of long paths."""
    home = str(Path.home())
    if value.startswith(home):
        value = "~" + value[len(home):]
    if len(value) > 64 and "/" in value:
        parts = value.split("/")
        if len(parts) > 4:
            value = "…/" + "/".join(parts[-3:])
    return value


def _first_line(value: str, limit: int) -> str:
    for line in str(value).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("/") and " " not in line:
            line = _short_path(line)
        return _trim(line, limit)
    return ""


def _tool_input_brief(tool_input: dict[str, Any]) -> str:
    # This engine's parameter names, in the order that identifies a call best.
    for key in ("command", "file_path", "pattern", "path", "query", "url"):
        value = tool_input.get(key)
        if value:
            text = str(value)
            if key in ("file_path", "path"):
                text = _short_path(text)
            return _trim(text.replace("\n", " "), _INPUT_LIMIT)
    return _trim(json.dumps(tool_input, ensure_ascii=False), _INPUT_LIMIT) if tool_input else ""
