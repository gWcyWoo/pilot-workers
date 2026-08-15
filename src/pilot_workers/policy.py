#!/usr/bin/env python3
"""Mode policy: agents, shell permission rules, prompts, and runner config.

Permission-matching facts (read from the pinned OpenCode binary,
confirmed by live probes):

- Resolution is LAST-MATCH-WINS (`findLast` over insertion-ordered rules).
  Deny rules must be inserted AFTER the allow rules they override.
- Each bash command is parsed with tree-sitter; every AST command node is
  checked separately. Operator-symbol rules (`*|*`, `*&&*`, etc.) are dead
  weight — each segment already hits the real allow/deny on its own.
- Redirected statements match with FULL text, so read-only modes keep a
  `*>*` deny inserted LAST to block redirect writes.

This layer guards against mistakes, not malicious models. Not an OS sandbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

from pilot_workers.providers import Provider

PERMISSIONS_DIR = Path(__file__).resolve().parent / "data" / "permissions"

VALID_MODES = ("code", "explore", "test", "review", "discuss")

MODE_TO_AGENT = {
    "code": "worker-code",
    "explore": "worker-explore",
    "test": "worker-test",
    "review": "worker-review",
    "discuss": "worker-discuss",
    "resume": "worker-code",
}

STEPS_BY_MODE = {"code": 120, "resume": 120, "review": 120, "explore": 80,
                 "test": 80, "discuss": 80}

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt(mode: str) -> str:
    prompt_mode = "code" if mode == "resume" else mode
    parts = []
    for name in ("common.md", f"{prompt_mode}.md"):
        path = PROMPTS_DIR / name
        if not path.is_file():
            raise RuntimeError(f"worker prompt file missing: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"worker prompt file is empty: {path}")
        parts.append(text)
    return "\n\n".join(parts)


# Path-shaped denies. Unlike every other rule here these match anywhere in the
# command rather than being anchored to a command name, so a later allow for an
# UNRELATED command shadows them under last-match-wins. Named separately
# because each mode that appends allows after the deny block has to put these
# back last (see test_shell_permissions).
CREDENTIAL_PATH_DENIES = ("*auth.json*", "*.env*")


def denied_shell_patterns() -> dict[str, str]:
    return {
        "git push*": "deny",
        "git pull*": "deny",
        "git fetch*": "deny",
        "git clone*": "deny",
        "git remote add*": "deny",
        "gh *": "deny",
        "curl *": "deny",
        "wget *": "deny",
        "ssh *": "deny",
        "scp *": "deny",
        "sftp *": "deny",
        "rsync *": "deny",
        "nc *": "deny",
        "ncat *": "deny",
        "npm publish*": "deny",
        "pnpm publish*": "deny",
        "yarn publish*": "deny",
        "rm -rf /*": "deny",
        "rm -rf ~*": "deny",
        "sudo *": "deny",
        **{pattern: "deny" for pattern in CREDENTIAL_PATH_DENIES},
    }


def readonly_shell_permissions() -> dict[str, str]:
    rules = {
        "*": "deny",
        "pwd": "allow",
        "ls*": "allow",
        "cat *": "allow",
        "echo *": "allow",
        "find *": "allow",
        "rg *": "allow",
        "grep *": "allow",
        "head *": "allow",
        "tail *": "allow",
        "wc *": "allow",
        "file *": "allow",
        "stat *": "allow",
        "npx tsc": "allow",
        "npx tsc *": "allow",
        "git status*": "allow",
        "git diff*": "allow",
        "git log*": "allow",
        "git show*": "allow",
        "git blame*": "allow",
        "git branch*": "allow",
        "git grep*": "allow",
        "git rev-parse*": "allow",
        "git ls-files*": "allow",
    }
    rules.update(denied_shell_patterns())
    # Exec-capable escapes: these run arbitrary commands from inside their own
    # argument/script text (awk system() and |getline, sed e, find -exec, git
    # pager/output flags), which no glob over that text can make read-only —
    # denied outright. Interpreter/forwarder denies are explicit even though
    # "*" already denies them, so a future allow can't silently reopen them.
    rules.update(
        {
            "awk *": "deny",
            "gawk *": "deny",
            "sed *": "deny",
            "perl *": "deny",
            "python*": "deny",
            "ruby *": "deny",
            "node *": "deny",
            "xargs *": "deny",
            "sh *": "deny",
            "bash *": "deny",
            "zsh *": "deny",
            "find *-exec*": "deny",
            "find *-ok*": "deny",
            "find *-delete*": "deny",
            "find *-fprint*": "deny",
            "find *-fls*": "deny",
            "git *--output*": "deny",
            "git grep *-O*": "deny",
            "git grep *--open-files-in-pager*": "deny",
        }
    )
    rules["*>*"] = "deny"
    return rules


def test_shell_permissions() -> dict[str, str]:
    rules = readonly_shell_permissions()
    rules.update(
        {
            "npm test*": "allow",
            "npm run test*": "allow",
            "npm run lint*": "allow",
            "npm run typecheck*": "allow",
            "npm run build*": "allow",
            "pnpm test*": "allow",
            "pnpm run test*": "allow",
            "pnpm run lint*": "allow",
            "pnpm run typecheck*": "allow",
            "pnpm run build*": "allow",
            "yarn test*": "allow",
            "yarn lint*": "allow",
            "yarn build*": "allow",
            "bun test*": "allow",
            "pytest*": "allow",
            "python -m pytest*": "allow",
            "go test*": "allow",
            "cargo test*": "allow",
            "npx vitest run*": "allow",
            "dart test*": "allow",
            "flutter test*": "allow",
            "make test*": "allow",
            "make check*": "allow",
            "make lint*": "allow",
            "just test*": "allow",
            "composer test*": "allow",
            "php artisan test*": "allow",
            "mvn test*": "allow",
            "./gradlew test*": "allow",
        }
    )
    # Re-append the PATH-shaped denies so they land after the runner allows.
    # `dict.update` keeps an existing key's original position, so calling
    # `denied_shell_patterns()` again here changed nothing at all — proven by
    # comparing the result with and without it, values and order identical —
    # while reading like a re-assertion of the denies. Without this, `pytest
    # .env` and `npm run build config/auth.json` resolved to ALLOW in test
    # mode, the one mode where those denies went inert; code and review both
    # denied them.
    #
    # Only these two. Re-appending the whole deny set would move `python*`
    # after `python -m pytest*` and break the deliberate override above.
    for pattern in CREDENTIAL_PATH_DENIES:
        del rules[pattern]
        rules[pattern] = "deny"
    # The redirect deny stays LAST of all, or "pytest > file" resolves to the
    # earlier "pytest*" allow.
    del rules["*>*"]
    rules["*>*"] = "deny"
    return rules


def code_shell_permissions() -> dict[str, str]:
    rules = {"*": "allow"}
    rules.update(denied_shell_patterns())
    return rules


# The file tools that read project content by path, and therefore need the same
# path denies bash has. Named so the profile merge can re-assert the floor
# without re-deriving which tools it applies to.
FILE_TOOLS = ("read", "edit", "grep")


def file_tool_path_rules() -> dict[str, str]:
    """Allow every path except the credential ones.

    The native file tools need the SAME path denies as bash. Granting them a
    bare "allow" made the shell's rules a facade: `cat .env` was refused while
    the read tool opened the same file and returned its contents, and grep
    returned the matching line — verified with a decoy .env in a throwaway
    workdir. Every mode is affected, read-only ones included, and
    prompts/common.md promises the opposite.
    """
    return {
        "*": "allow",
        **{pattern: "deny" for pattern in CREDENTIAL_PATH_DENIES},
        # Kept alongside the broad `*.env*` rather than folded into it: the
        # engine's file-tool matcher is not guaranteed to be the shell's, and
        # these two anchor the common shapes explicitly.
        "*.env": "deny",
        "*.env.*": "deny",
    }


def agent_permissions(mode: str) -> dict[str, Any]:
    effective_mode = "code" if mode == "resume" else mode
    editable = effective_mode == "code"
    if effective_mode == "code":
        bash = code_shell_permissions()
    elif effective_mode == "test":
        bash = test_shell_permissions()
    else:
        bash = readonly_shell_permissions()
    file_tool_rules = file_tool_path_rules()
    return {
        "*": "deny",
        "read": dict(file_tool_rules),
        "edit": (dict(file_tool_rules) if editable else "deny"),
        "glob": "allow",
        "grep": dict(file_tool_rules),
        "list": "allow",
        "bash": bash,
        "task": "deny",
        "todowrite": "allow",
        "webfetch": "deny",
        "websearch": "deny",
        "external_directory": "deny",
        "lsp": "allow",
        "skill": "deny",
        "question": "deny",
        "doom_loop": "deny",
        "mcp_*": "deny",
    }


def _validate_profile_shape(data: Any, path: Path) -> dict[str, Any]:
    """Validate the {mode|_all: {shell|tools: {pattern: action}}} shape.

    Shared by packaged profiles and user-level permission overrides — the two
    stores carry the same structure, so a malformed file fails with the same
    message either way.
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"permission profile must be a mapping: {path}")
    for key, section in data.items():
        if key != "_all" and key not in VALID_MODES:
            raise RuntimeError(
                f"permission profile has unknown section '{key}' "
                f"(expected _all or one of {VALID_MODES}): {path}"
            )
        # Shape, not just name. `code: allow` was skipped in silence by the merge
        # (it tests isinstance(section, dict) and moves on) and
        # `code: {shell: allow}` raised AttributeError from inside it - a
        # traceback for a typo in the operator's own config file.
        if not isinstance(section, dict):
            raise RuntimeError(
                f"permission profile section '{key}' must be a mapping with "
                f"'shell' and/or 'tools' keys, got "
                f"{type(section).__name__}: {path}"
            )
        unknown = set(section) - {"shell", "tools"}
        if unknown:
            raise RuntimeError(
                f"permission profile section '{key}' has unknown keys "
                f"{sorted(unknown)} (expected shell, tools): {path}"
            )
        for field in ("shell", "tools"):
            if field in section and not isinstance(section[field], dict):
                raise RuntimeError(
                    f"permission profile '{key}.{field}' must be a mapping of "
                    f"pattern -> action, got "
                    f"{type(section[field]).__name__}: {path}"
                )
    return data


def load_permission_profile(name: str) -> dict[str, Any]:
    """Load a permission profile YAML from the permissions/ directory."""
    path = PERMISSIONS_DIR / f"{name}.yaml"
    if not path.is_file():
        raise RuntimeError(f"permission profile not found: {path}")
    if yaml is None:
        raise RuntimeError(
            "pyyaml is required for custom permission profiles; "
            "install it with: pip install pyyaml"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _validate_profile_shape(data, path)


def permission_overrides_path(provider_key: str) -> Path:
    """User-level per-provider permission overrides, outside package data.

    JSON rather than YAML on purpose: the file is machine-written by the
    `permissions` CLI and machine-read here, and stdlib json keeps the load
    path working without pyyaml (which stays optional).
    """
    from pilot_workers.providers import pilot_home

    return pilot_home() / "permissions" / f"{provider_key}.json"


def load_permission_overrides(provider_key: str) -> dict[str, Any] | None:
    """Load a provider's user-level overrides, or None when absent.

    Same shape as a packaged profile; a corrupt file raises rather than
    silently dispatching with fewer rules than the operator configured.
    """
    import json

    path = permission_overrides_path(provider_key)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        # An unreadable file must not traceback out of the CLI nor dispatch
        # with fewer rules than configured — same contract as corrupt JSON.
        raise RuntimeError(f"cannot read permission overrides: {path}: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"permission overrides are not valid JSON: {path}: {exc}")
    return _validate_profile_shape(data, path)


def _merge_permissions(
    base: dict[str, Any], profile: dict[str, Any] | None, mode: str,
) -> dict[str, Any]:
    """Merge a permission profile into base agent permissions.

    Shell rules from the profile are appended after the base rules
    (last-match-wins in OpenCode). Tool rules overwrite base values.
    """
    if profile is None:
        return base

    sections = []
    if "_all" in profile:
        sections.append(profile["_all"])
    effective_mode = "code" if mode == "resume" else mode
    if effective_mode in profile:
        sections.append(profile[effective_mode])
    if not sections:
        return base

    # A SHALLOW copy shares every nested dict with the caller, and the file-tool
    # floor below pops and re-inserts deny patterns in place — so merging a
    # profile silently reordered the caller's own base map. Harmless today
    # (agent_permissions returns a fresh dict per call) and a trap for the first
    # caller that reuses one.
    result = {key: (dict(value) if isinstance(value, dict) else value)
              for key, value in base.items()}
    # A prior layer may have set bash WHOLESALE to a scalar ("deny") via
    # `tools: {bash: ...}` — the early-return path below stores it unwrapped,
    # and `dict()` on that string crashed the SECOND merge (profile then user
    # overrides). Synthesize a map from the scalar, carrying the MODE's own
    # guardrail denies: the re-pin loop below only re-pins keys that exist
    # (code mode legitimately has no `*>*` deny), so a bare {"*": scalar}
    # seed would let this layer's pattern allows widen past the mode floor.
    prior_bash = result.get("bash", {})
    if isinstance(prior_bash, dict):
        bash_rules = dict(prior_bash)
    else:
        bash_rules = {"*": prior_bash}
        mode_base = agent_permissions(mode).get("bash", {})
        for pattern in (*CREDENTIAL_PATH_DENIES, "*>*"):
            if mode_base.get(pattern) == "deny":
                bash_rules[pattern] = "deny"

    for section in sections:
        if not isinstance(section, dict):
            continue
        for pattern, action in (section.get("shell") or {}).items():
            bash_rules[pattern] = action
        for tool, action in (section.get("tools") or {}).items():
            if tool == "bash":
                # A wholesale bash override via tools: {bash: ...} must flow
                # through bash_rules (not result["bash"]) so the re-pin loop
                # below can re-assert guardrails. A scalar ("deny") is
                # converted to a seed map; a dict is merged on top.
                if isinstance(action, dict):
                    bash_rules.update(action)
                else:
                    bash_rules = {"*": action}
                    mode_base = agent_permissions(mode).get("bash", {})
                    for pattern in (*CREDENTIAL_PATH_DENIES, "*>*"):
                        if mode_base.get(pattern) == "deny":
                            bash_rules[pattern] = "deny"
            else:
                result[tool] = action

    # Profile shell rules are APPENDED, so a pattern the base does not already
    # have lands after the two ordering-sensitive denies that have to come last.
    # With `"jq *": allow`, `jq . data.json > overwrite.json` resolved to allow
    # in review mode — a write in a read-only mode, and `*>*` is documented at
    # the top of this module as necessarily last. The bundled `relaxed` profile
    # does NOT trip it: its patterns exist in the base, and dict assignment
    # keeps an existing key's position — which is why reading it proved nothing.
    #
    # Judged on the EFFECTIVE value, not on whether the profile mentioned the
    # pattern. Skipping every pattern the profile named was too broad: a profile
    # that names `*.env*: deny` (agreeing with the floor) AND adds a new allow
    # left the deny at its base position, where the new allow outranked it. A
    # profile that widened it to `allow` is the deliberate override and shows up
    # here as a non-deny value, which is the only case worth skipping.
    for pattern in (*CREDENTIAL_PATH_DENIES, "*>*"):
        if bash_rules.get(pattern) != "deny":
            continue
        del bash_rules[pattern]
        bash_rules[pattern] = "deny"

    # A profile widening a file TOOL must not discard that tool's PATH denies.
    # `data/permissions/README.md` documents `explore: tools: edit: allow`, and
    # taking it literally replaced the whole rule map with a bare "allow" —
    # silently reopening the credential-path bypass, and making the promise in
    # prompts/common.md false for that run.
    #
    # Widening a NAMED path pattern under `shell:` stays honoured: that is a
    # deliberate act (it is how the bundled `relaxed` profile re-allows curl),
    # not a side effect of asking for a capability. Tightening a tool to "deny"
    # is likewise untouched.
    for tool in FILE_TOOLS:
        value = result.get(tool)
        if not isinstance(value, (str, dict)):
            # Non-string, non-dict values (True, int, list) are not valid
            # OpenCode tool actions; treat as "allow" so the floor applies.
            result[tool] = file_tool_path_rules()
        elif value == "allow":
            result[tool] = file_tool_path_rules()
        elif isinstance(value, dict):
            for pattern, action in file_tool_path_rules().items():
                if action == "deny":
                    value.pop(pattern, None)
                    value[pattern] = action   # last-match-wins: denies go last

    # Always write the merged bash rules back — even when a profile set bash
    # wholesale via `tools: {bash: ...}`. The early return skipped this
    # assignment, so the guardrail re-pin above was discarded and the raw
    # override went through with no redirect/credential denies.
    result["bash"] = bash_rules
    return result


def effective_permissions(
    provider: Provider, mode: str, *, permission_profile: str | None = None,
) -> dict[str, Any]:
    """Built-in mode defaults → named profile → user-level overrides.

    The overrides layer is merged last, so an operator's `permissions add`
    beats package defaults; each merge re-pins the redirect/credential denies
    and the file-tool floor, so an added allow cannot displace them.
    """
    profile_name = permission_profile or provider.permissions
    profile = load_permission_profile(profile_name) if profile_name else None
    permissions = _merge_permissions(agent_permissions(mode), profile, mode)
    return _merge_permissions(
        permissions, load_permission_overrides(provider.key), mode)


def build_config(provider: Provider, mode: str, *, permission_profile: str | None = None) -> dict[str, Any]:
    agent_name = MODE_TO_AGENT[mode]
    prompt_mode = "code" if mode == "resume" else mode
    model = provider.model
    permissions = effective_permissions(
        provider, mode, permission_profile=permission_profile)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": model,
        "small_model": model,
        "default_agent": agent_name,
        "enabled_providers": [provider.provider_id],
        "permission": {"*": "deny"},
        "agent": {
            agent_name: {
                "description": f"Isolated {prompt_mode} worker controlled by the main planner",
                "mode": "primary",
                "model": model,
                "steps": STEPS_BY_MODE[mode],
                "prompt": load_prompt(mode),
                "permission": permissions,
            }
        },
    }
    if provider.auth == "api":
        # A key-authenticated provider is declared in full: the engine has no
        # built-in entry for it, so this block IS the endpoint and model card.
        config["provider"] = {
            provider.provider_id: {
                "npm": provider.npm,
                "name": provider.display_name,
                "options": {"baseURL": provider.base_url},
                "models": {
                    provider.model_id: {
                        "name": provider.display_name,
                        "limit": {
                            "context": provider.context_tokens,
                            "output": provider.output_tokens,
                        },
                    }
                },
            }
        }
    # An oauth provider declares nothing: the engine owns the integration
    # (endpoint, model catalogue and token refresh), and a custom block here
    # would shadow it with a half-configured duplicate.
    return config
