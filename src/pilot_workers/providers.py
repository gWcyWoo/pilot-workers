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


def _require_int(value: Any, field: str, path: Path) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        raise RuntimeError(f"provider {path.name}: {field} must be an integer, got {value!r}")


def load_providers(providers_dir: Path | None = None) -> dict[str, Provider]:
    """Discover and load all provider YAML files from the given directory."""
    directory = providers_dir or PROVIDERS_DIR
    if not directory.is_dir():
        raise RuntimeError(f"providers directory does not exist: {directory}")
    providers: dict[str, Provider] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = _parse_yaml(path)
        required = ("key", "provider_id", "model_id", "base_url", "display_name",
                     "context_tokens", "output_tokens")
        missing = [f for f in required if f not in data]
        if missing:
            raise RuntimeError(f"provider {path.name} missing fields: {', '.join(missing)}")
        if not isinstance(data["key"], str):
            raise RuntimeError(f"provider {path.name}: key must be a string, got {type(data['key']).__name__}")
        if data["key"] in RESERVED_PROVIDER_KEYS:
            raise RuntimeError(
                f"provider {path.name} uses reserved key: {data['key']}"
            )
        provider = Provider(
            key=data["key"],
            provider_id=data["provider_id"],
            model_id=data["model_id"],
            base_url=data["base_url"],
            display_name=data["display_name"],
            context_tokens=_require_int(data["context_tokens"], "context_tokens", path),
            output_tokens=_require_int(data["output_tokens"], "output_tokens", path),
            permissions=data.get("permissions") or None,
            runner=data.get("runner") or "opencode",
            asset_prefix=data.get("asset_prefix") or data["key"],
            strengths=str(data.get("strengths") or ""),
            suitable_modes=str(data.get("suitable_modes") or ""),
            notes=str(data.get("notes") or ""),
        )
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
        data = _parse_yaml(path)
        if "key" not in data:
            continue
        if not isinstance(data["key"], str):
            continue
        if data["key"] in RESERVED_PROVIDER_KEYS:
            continue
        required = ("key", "provider_id", "model_id", "base_url", "display_name",
                     "context_tokens", "output_tokens")
        missing = [f for f in required if f not in data]
        if missing:
            continue
        result[data["key"]] = Provider(
            key=data["key"],
            provider_id=data["provider_id"],
            model_id=data["model_id"],
            base_url=data["base_url"],
            display_name=data["display_name"],
            context_tokens=_require_int(data["context_tokens"], "context_tokens", path),
            output_tokens=_require_int(data["output_tokens"], "output_tokens", path),
            permissions=data.get("permissions") or None,
            runner=data.get("runner") or "opencode",
            asset_prefix=data.get("asset_prefix") or data["key"],
            strengths=str(data.get("strengths") or ""),
            suitable_modes=str(data.get("suitable_modes") or ""),
            notes=str(data.get("notes") or ""),
        )
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
