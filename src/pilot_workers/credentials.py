#!/usr/bin/env python3
"""Store worker credentials in provider-isolated auth files."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import tempfile

from pilot_workers.providers import PROVIDERS, workers_root
from pilot_workers.runners import get_runner


def credential_status(provider_key: str) -> dict[str, object]:
    provider = PROVIDERS[provider_key]
    runner = get_runner(provider.runner)
    path = runner.credential_path(provider)
    configured = False
    secure_mode = False
    error: str | None = None
    if path.is_file():
        secure_mode = (path.stat().st_mode & 0o077) == 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            try:
                key = runner.parse_credential(provider, payload)
            except (RuntimeError, TypeError, AttributeError) as exc:
                error = str(exc)
                key = ""
            configured = bool(key and key.strip())
        except (OSError, json.JSONDecodeError) as exc:
            error = str(exc)
    return {
        "provider": provider_key,
        "runner": provider.runner,
        "provider_id": provider.provider_id,
        "configured": configured,
        "secure_mode": secure_mode,
        "path": str(path),
        "error": error,
    }


def ensure_private_directories(destination) -> None:
    current = workers_root()
    target = destination.parent
    while True:
        current.mkdir(mode=0o700, parents=True, exist_ok=True)
        current.chmod(0o700)
        if current == target:
            return
        current = current / target.relative_to(current).parts[0]


def configure(provider_key: str) -> None:
    provider = PROVIDERS[provider_key]
    runner = get_runner(provider.runner)
    key = getpass.getpass(f"{provider_key} API key (input hidden): ").strip()
    if not key:
        raise SystemExit(f"error: empty {provider_key} API key; no file was changed")
    destination = runner.credential_path(provider)
    ensure_private_directories(destination)
    payload = runner.credential_payload(provider, key)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=".auth.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.chmod(temporary_name, 0o600)
            json.dump(payload, temporary, separators=(",", ":"))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        os.chmod(destination, 0o600)
    finally:
        key = ""
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(f"Configured {provider_key} in {destination} (mode 0600); key not displayed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure isolated worker credentials.")
    parser.add_argument("provider", choices=[*sorted(PROVIDERS), "all"])
    parser.add_argument("--status", action="store_true", help="Show metadata only; never show keys.")
    args = parser.parse_args()
    keys = sorted(PROVIDERS) if args.provider == "all" else [args.provider]
    if args.status:
        print(json.dumps([credential_status(item) for item in keys], indent=2))
        return 0
    for provider_key in keys:
        configure(provider_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
