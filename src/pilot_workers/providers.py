#!/usr/bin/env python3
"""Provider registry: loads provider definitions from YAML files.

The single source of truth for provider routing is the `data/providers/`
directory inside this package. Each `.yaml` file defines one provider.
Adding a new model only requires dropping a new YAML file — no Python changes.

The shared path helpers also live here so that every other module imports
facts from one place.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]


MAX_TASK_BYTES = 512_000

PROVIDERS_DIR = Path(__file__).resolve().parent / "data" / "providers"

# Keys that collide with CLI grammar (`install runner`, `uninstall key`);
# a provider YAML using one of these would make the CLI ambiguous.
RESERVED_PROVIDER_KEYS = frozenset({"runner", "key"})


def credential_setup_hint(provider_key: str) -> str:
    """The one sentence that tells a user how to supply a provider's API key."""
    return f"run: pw9 key {provider_key}"


@dataclass(frozen=True)
class Provider:
    key: str
    provider_id: str
    model_id: str
    base_url: str
    display_name: str
    context_tokens: int
    output_tokens: int
    permissions: str | None = None
    runner: str = "opencode"
    asset_prefix: str = ""
    # Engine wiring. The defaults reproduce the original hardcoded values, so a
    # provider YAML that names none of them builds exactly the config it did
    # before these fields existed.
    npm: str = "@ai-sdk/openai-compatible"
    # Whether the engine needs this provider spelled out. A provider the
    # engine already knows (its id is in the engine's own registry, e.g.
    # `openai`, `anthropic`) carries its own endpoint, npm package and model
    # catalogue there; declaring a second, thinner copy here would shadow it.
    # Custom endpoints (a vendor's coding-plan URL under a made-up id) are
    # NOT in that registry and must be declared — hence the default.
    declare: bool = True
    # "api" (key in auth.json) or "oauth" (the engine owns the login flow and
    # writes its own token payload). An oauth provider needs no base_url: the
    # engine's built-in integration carries the endpoint.
    auth: str = "api"
    auth_method: str = ""
    # Reasoning budget for models that take one, passed through to the engine
    # as the agent's `reasoningEffort` option. Empty means "say nothing" and
    # the engine's own default applies — which is what a non-reasoning model
    # needs, since sending the option at all is an error there.
    effort: str = ""
    # v0.5.0 (design D1): optional flat metadata surfaced by `status` and
    # consulted when picking a provider for a mode. All default to "".
    strengths: str = ""
    suitable_modes: str = ""
    notes: str = ""

    @property
    def model(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


def _fallback_scalar(raw: str, path: Path, line: str) -> str:
    """Read one flat scalar the way PyYAML reads it.

    ``pyyaml`` is optional, so the same file must yield the same provider with
    or without it. Two rules are enough for these flat files, and both matter
    for the template ``data/providers/README.md`` tells an author to copy —
    every one of its seven fields carries a trailing ``# comment``:

    - a leading quote ends the scalar at its closing quote; the rest is comment
    - otherwise a ``#`` preceded by whitespace starts a comment (``glm#5``
      keeps its hash, ``model #1`` does not)

    An unterminated quote is a ScannerError to pyyaml, so it is an error here
    too: keeping ``"myprov`` as a provider key — which is then used verbatim as
    a directory name — is exactly the silent corruption these rules remove.
    """
    value = raw.strip()
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            raise RuntimeError(
                f"provider {path.name}: unterminated quote in line: {line.strip()}"
            )
        return value[1:end]
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index].rstrip()
    return value


def _parse_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file, with a stdlib fallback for simple flat files."""
    # utf-8-sig: pyyaml strips a leading BOM, the fallback did not, so an editor
    # that saves with one turned the first key into "﻿key" and the file
    # failed as "missing fields: key" — another way for the two parsers to read
    # one file differently.
    text = path.read_text(encoding="utf-8-sig")
    if yaml is not None:
        result = yaml.safe_load(text)
        if not isinstance(result, dict):
            return {}
        return result
    # Minimal fallback: flat key: value files without nesting.
    result: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        # Comments and quotes come off BEFORE the numeric check: pyyaml reads
        # `context_tokens: 128000   # max context window` as the int 128000.
        value = _fallback_scalar(value, path, line)
        if value.isdigit():
            result[key.strip()] = int(value)
        else:
            result[key.strip()] = value
    return result


def _require_bool(value: Any, default: bool) -> bool:
    """YAML `false`/`true`, or the strings a flat-parser fallback produces."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "no", "0")


def _require_int(value: Any, field: str, path: Path) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        raise RuntimeError(f"provider {path.name}: {field} must be an integer, got {value!r}")


AUTH_MODES = ("api", "oauth")

# The union OpenCode 1.18.4 accepts; which subset a given model takes is the
# model's business, so an unsupported-but-spelled-correctly value is left for
# the engine to reject loudly rather than guessed at here.
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def _require_effort(value: Any, path: Path) -> str:
    effort = str(value or "").strip().lower()
    if effort and effort not in EFFORT_LEVELS:
        raise RuntimeError(
            f"provider {path.name}: effort must be one of "
            f"{', '.join(EFFORT_LEVELS)}, got {effort!r}")
    return effort


def provider_from_data(data: dict[str, Any], path: Path) -> Provider:
    """Validate one provider mapping and build a Provider.

    The single construction site: the packaged loader and the user-override
    loader both come through here, so a new field cannot reach one and miss
    the other.
    """
    auth = str(data.get("auth") or "api")
    if auth not in AUTH_MODES:
        raise RuntimeError(
            f"provider {path.name}: auth must be one of "
            f"{', '.join(AUTH_MODES)}, got {auth!r}")
    # An oauth provider takes its endpoint from the engine's built-in
    # integration, so base_url is meaningless there and must not be demanded.
    # `declare: false` says the same thing for a key-authenticated provider
    # the engine already knows.
    required = ["key", "provider_id", "model_id", "display_name",
                "context_tokens", "output_tokens"]
    declare = _require_bool(data.get("declare"), True)
    if auth == "api" and declare:
        required.insert(3, "base_url")
    missing = [f for f in required if f not in data]
    if missing:
        raise RuntimeError(
            f"provider {path.name} missing fields: {', '.join(missing)}")
    if not isinstance(data["key"], str):
        raise RuntimeError(
            f"provider {path.name}: key must be a string, "
            f"got {type(data['key']).__name__}")
    if data["key"] in RESERVED_PROVIDER_KEYS:
        raise RuntimeError(
            f"provider {path.name} uses reserved key: {data['key']}")
    if auth == "oauth" and not str(data.get("auth_method") or "").strip():
        raise RuntimeError(
            f"provider {path.name}: auth: oauth requires auth_method "
            f"(the engine's login method id)")
    return Provider(
        key=data["key"],
        provider_id=data["provider_id"],
        model_id=data["model_id"],
        base_url=str(data.get("base_url") or ""),
        display_name=data["display_name"],
        context_tokens=_require_int(data["context_tokens"], "context_tokens", path),
        output_tokens=_require_int(data["output_tokens"], "output_tokens", path),
        permissions=data.get("permissions") or None,
        runner=data.get("runner") or "opencode",
        asset_prefix=data.get("asset_prefix") or data["key"],
        npm=str(data.get("npm") or "@ai-sdk/openai-compatible"),
        declare=_require_bool(data.get("declare"), True),
        auth=auth,
        auth_method=str(data.get("auth_method") or ""),
        effort=_require_effort(data.get("effort"), path),
        strengths=str(data.get("strengths") or ""),
        suitable_modes=str(data.get("suitable_modes") or ""),
        notes=str(data.get("notes") or ""),
    )


def load_providers(providers_dir: Path | None = None) -> dict[str, Provider]:
    """Discover and load all provider YAML files from the given directory."""
    directory = providers_dir or PROVIDERS_DIR
    if not directory.is_dir():
        raise RuntimeError(f"providers directory does not exist: {directory}")
    providers: dict[str, Provider] = {}
    for path in sorted(directory.glob("*.yaml")):
        provider = provider_from_data(_parse_yaml(path), path)
        if provider.key in providers:
            raise RuntimeError(f"duplicate provider key: {provider.key}")
        providers[provider.key] = provider
    if not providers:
        raise RuntimeError(f"no provider YAML files found in {directory}")
    return providers


def pilot_home() -> Path:
    """Root for all pilot-workers runtime data (credentials, logs, worktrees)."""
    return Path(os.environ.get("PILOT_WORKERS_HOME",
                os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()


def user_providers_dir() -> Path:
    """User-level provider overrides, outside package data."""
    return pilot_home() / "providers"


def _load_user_overrides() -> dict[str, Provider]:
    """Load user-level provider YAMLs that override or extend package defaults."""
    d = user_providers_dir()
    if not d.is_dir():
        return {}
    result: dict[str, Provider] = {}
    for path in sorted(d.glob("*.yaml")):
        try:
            provider = provider_from_data(_parse_yaml(path), path)
        except (RuntimeError, OSError):
            # A malformed user override is skipped rather than fatal: it must
            # not make every command unusable until the file is hand-fixed.
            continue
        result[provider.key] = provider
    return result


def _merge_providers() -> dict[str, Provider]:
    """Package defaults + user overrides (user wins on same key)."""
    base = load_providers()
    base.update(_load_user_overrides())
    return base


# Eagerly loaded at import time for backwards compatibility with existing code
# that does `from providers import PROVIDERS`. Callers that need a custom
# directory can call load_providers() directly.
PROVIDERS = _merge_providers()


def reload_providers() -> dict[str, Provider]:
    """Re-resolve PROVIDERS against the CURRENT pilot home, in place.

    The user-override layer is read at import, so a process that changes
    ``PILOT_WORKERS_HOME``/``CODEX_HOME`` afterwards keeps whatever home was
    in effect when this module first loaded. That is right for a normal run
    (one home, imported once) and wrong for the test suite, which redirects
    the home per test AFTER import — without this, every test would see the
    developer's own ``~/.codex/providers/`` and pass or fail on their
    personal config.

    Mutates the existing dict rather than rebinding: callers do
    ``from providers import PROVIDERS`` and hold the object itself.
    """
    fresh = _merge_providers()
    PROVIDERS.clear()
    PROVIDERS.update(fresh)
    return PROVIDERS


def workers_root() -> Path:
    return pilot_home() / "opencode-workers"


def profile_root(provider: Provider) -> Path:
    return workers_root() / "providers" / provider.key


def profile_paths(provider: Provider) -> dict[str, Path]:
    root = profile_root(provider)
    return {
        "root": root,
        "config": root / "config",
        "data": root / "data",
        "state": root / "state",
        "cache": root / "cache",
    }


def runs_root(provider: Provider) -> Path:
    return profile_root(provider) / "runs"


def run_paths(provider: Provider, run_id: str) -> dict[str, Path]:
    root = runs_root(provider) / run_id
    return {
        "root": root,
        "config": root / "config",
        "data": root / "data",
        "state": root / "state",
        "cache": root / "cache",
        "lock": root / ".lock",
    }


def logs_root(provider: Provider) -> Path:
    return workers_root() / "logs" / provider.key


def worktrees_root() -> Path:
    return workers_root() / "worktrees"
