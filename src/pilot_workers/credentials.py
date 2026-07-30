#!/usr/bin/env python3
"""Store worker credentials in provider-isolated auth files."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import tempfile
import time

from pilot_workers.providers import PROVIDERS, workers_root
from pilot_workers.runners import get_runner


# How long a ``.auth.*.tmp`` must sit untouched before it counts as abandoned
# rather than another writer's work in progress. A key write is a few
# milliseconds; a minute is a generous margin either way.
STALE_TMP_GRACE_SECONDS = 60


def ensure_private_directories(destination: Path) -> None:
    current = workers_root()
    target = destination.parent
    while True:
        current.mkdir(mode=0o700, parents=True, exist_ok=True)
        current.chmod(0o700)
        if current == target:
            return
        current = current / target.relative_to(current).parts[0]


def configure(provider_key: str) -> None:
    """Prompt for a provider's API key and write it atomically at mode 0600.

    Raises ``RuntimeError`` rather than ``SystemExit``: callers turn a
    RuntimeError into a clean ``error:`` line and a non-zero exit code.
    """
    if provider_key not in PROVIDERS:
        # A YAML deleted mid-flight would otherwise raise KeyError, which the
        # callers' ``except (OSError, RuntimeError)`` does not catch. Reject
        # BEFORE prompting so no key is collected for a provider being refused.
        raise RuntimeError(
            f"unknown provider {provider_key!r}; "
            f"choose from {', '.join(sorted(PROVIDERS))}"
        )
    provider = PROVIDERS[provider_key]
    runner = get_runner(provider.runner)
    try:
        key = getpass.getpass(f"{provider_key} API key (input hidden): ").strip()
    except EOFError:
        # No stdin at all (CI, a closed pipe): a traceback here would bury the
        # actual problem, which is that this command needs a terminal.
        raise RuntimeError(
            f"cannot prompt for the {provider_key} API key: no terminal. "
            f"Run this from an interactive shell."
        ) from None
    if not key:
        raise RuntimeError(f"empty {provider_key} API key; no file was changed")
    destination = runner.credential_path(provider)
    ensure_private_directories(destination)
    # A hard crash (SIGKILL, power loss) runs no finally block, so a previous
    # attempt can have stranded a temp file still holding a key. It is 0600 in a
    # 0700 directory — the same protection as the destination — but sweep it
    # anyway rather than let secrets accumulate. Only tmps older than the grace
    # window: a second concurrent configure() of the same provider has a LIVE
    # tmp under this same glob, and sweeping it makes that writer's os.replace
    # fail for no reason.
    horizon = time.time() - STALE_TMP_GRACE_SECONDS
    for stale in destination.parent.glob(".auth.*.tmp"):
        try:
            if stale.stat().st_mtime > horizon:
                continue
            stale.unlink()
        except OSError:
            pass
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
