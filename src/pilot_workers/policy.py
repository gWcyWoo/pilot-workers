#!/usr/bin/env python3
"""Mode policy: agents, shell permission rules, prompts, and runner config.

Permission-matching facts (read from the pinned OpenCode 1.18.3 binary,
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

from pilot_workers.providers import Provider

MODE_TO_AGENT = {
    "code": "worker-code",
    "explore": "worker-explore",
    "test": "worker-test",
    "review": "worker-review",
    "resume": "worker-code",
}

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
        "git status*": "allow",
        "git diff*": "allow",
        "git log*": "allow",
        "git show*": "allow",
        "git blame*": "allow",
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


def build_config(provider: Provider, mode: str) -> dict[str, Any]:
    agent_name = MODE_TO_AGENT[mode]
    prompt_mode = "code" if mode == "resume" else mode
    model = provider.model
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
                "steps": 80,
                "prompt": load_prompt(mode),
                "permission": agent_permissions(mode),
            }
        },
    }
