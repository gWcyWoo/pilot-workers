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

VALID_MODES = ("code", "explore", "test", "review")

MODE_TO_AGENT = {
    "code": "worker-code",
    "explore": "worker-explore",
    "test": "worker-test",
    "review": "worker-review",
    "resume": "worker-code",
}

STEPS_BY_MODE = {"code": 120, "resume": 120, "review": 120, "explore": 80, "test": 80}

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
        "*auth.json*": "deny",
        "*.env*": "deny",
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
        "sed *": "allow",
        "awk *": "allow",
        "head *": "allow",
        "tail *": "allow",
        "wc *": "allow",
        "file *": "allow",
        "stat *": "allow",
        "npx tsc*": "allow",
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
    rules.update(denied_shell_patterns())
    return rules


def code_shell_permissions() -> dict[str, str]:
    rules = {"*": "allow"}
    rules.update(denied_shell_patterns())
    return rules


def agent_permissions(mode: str) -> dict[str, Any]:
    effective_mode = "code" if mode == "resume" else mode
    editable = effective_mode == "code"
    if effective_mode == "code":
        bash = code_shell_permissions()
    elif effective_mode == "test":
        bash = test_shell_permissions()
    else:
        bash = readonly_shell_permissions()
    return {
        "*": "deny",
        "read": "allow",
        "edit": "allow" if editable else "deny",
        "glob": "allow",
        "grep": "allow",
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
    if not isinstance(data, dict):
        raise RuntimeError(f"permission profile must be a YAML mapping: {path}")
    for key in data:
        if key != "_all" and key not in VALID_MODES:
            raise RuntimeError(
                f"permission profile has unknown section '{key}' "
                f"(expected _all or one of {VALID_MODES}): {path}"
            )
    return data


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

    result = dict(base)
    bash_rules = dict(result.get("bash", {}))

    for section in sections:
        if not isinstance(section, dict):
            continue
        for pattern, action in (section.get("shell") or {}).items():
            bash_rules[pattern] = action
        for tool, action in (section.get("tools") or {}).items():
            result[tool] = action

    result["bash"] = bash_rules
    return result


def build_config(provider: Provider, mode: str, *, permission_profile: str | None = None) -> dict[str, Any]:
    profile_name = permission_profile or provider.permissions
    profile = load_permission_profile(profile_name) if profile_name else None
    agent_name = MODE_TO_AGENT[mode]
    prompt_mode = "code" if mode == "resume" else mode
    model = provider.model
    permissions = _merge_permissions(agent_permissions(mode), profile, mode)
    return {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": model,
        "small_model": model,
        "default_agent": agent_name,
        "enabled_providers": [provider.provider_id],
        "provider": {
            provider.provider_id: {
                "npm": "@ai-sdk/openai-compatible",
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
        },
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
