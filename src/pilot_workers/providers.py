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

# Keys that collide with the install/uninstall CLI grammar
# ('install <provider> on <host>', 'install runner <name>'); a provider
# YAML using one of these would make the CLI ambiguous.
RESERVED_PROVIDER_KEYS = frozenset({"runner", "all", "on", "claude", "codex"})


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

    @property
    def model(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


def _parse_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML file, with a stdlib fallback for simple flat files."""
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        result = yaml.safe_load(text)
        if not isinstance(result, dict):
            return {}
        return result
    # Minimal fallback: flat key: value files without nesting.
    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
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
        )
        if provider.key in providers:
            raise RuntimeError(f"duplicate provider key: {provider.key}")
        providers[provider.key] = provider
    if not providers:
        raise RuntimeError(f"no provider YAML files found in {directory}")
    return providers


# Eagerly loaded at import time for backwards compatibility with existing code
# that does `from providers import PROVIDERS`. Callers that need a custom
# directory can call load_providers() directly.
PROVIDERS = load_providers()


def pilot_home() -> Path:
    """Root for all pilot-workers runtime data (credentials, logs, worktrees)."""
    return Path(os.environ.get("PILOT_WORKERS_HOME",
                os.environ.get("CODEX_HOME", Path.home() / ".codex"))).expanduser().resolve()


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


def logs_root(provider: Provider) -> Path:
    return workers_root() / "logs" / provider.key


def worktrees_root() -> Path:
    return workers_root() / "worktrees"
