#!/usr/bin/env python3
"""Install and uninstall: runner runtime and provider API keys.

Grammar:

    pw9 install runner <name>
    pw9 uninstall runner <name>
    pw9 key <provider>            # prompt for API key
    pw9 uninstall key <provider>  # delete API key
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from pilot_workers import providers

INSTALL_USAGE = """usage:
  pw9 install runner <name>
  pw9 uninstall runner <name>
"""

UNINSTALL_USAGE = """usage:
  pw9 uninstall runner <name>
  pw9 uninstall key <provider>
"""

KEY_USAGE = """usage:
  pw9 key <provider>
"""


# ------------------------------------------------------------------
# runner install / uninstall
# ------------------------------------------------------------------


def _install_runner(name: str) -> int:
    from pilot_workers.runners import RUNNERS, get_runner
    from pilot_workers.runners.opencode_runner import clear_version_cache

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    runner = get_runner(name)
    script = runner.install_script()
    if script is None:
        print(f"note: runner {name} needs no runtime install")
        return 0
    rc = subprocess.run(["bash", str(script)]).returncode
    if rc != 0:
        return rc
    clear_version_cache()
    runtime_root = runner.runtime_root()
    pinned = runner.pinned_version
    if runtime_root is not None and runtime_root.is_dir() and pinned:
        for child in sorted(runtime_root.iterdir()):
            if child.name != pinned:
                print(f"note: stale runner version present: {child}")
    return 0


def _uninstall_runner(name: str) -> int:
    from pilot_workers.runners import RUNNERS, get_runner

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    runtime_root = get_runner(name).runtime_root()
    if runtime_root is None:
        print(f"note: runner {name} has no runtime to remove")
        return 0
    if not runtime_root.exists():
        print(f"note: no runner install found at {runtime_root}")
        return 0
    for child in sorted(runtime_root.iterdir()):
        print(f"removed: {child}")
    shutil.rmtree(runtime_root)
    print(f"removed: {runtime_root}")
    return 0


# ------------------------------------------------------------------
# API key management
# ------------------------------------------------------------------


def _key_configure(provider_key: str) -> int:
    from pilot_workers import credentials

    if provider_key not in providers.PROVIDERS:
        print(f"error: unknown provider: {provider_key} "
              f"(available: {', '.join(sorted(providers.PROVIDERS))})",
              file=sys.stderr)
        return 2
    try:
        credentials.configure(provider_key)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _uninstall_key(provider_key: str) -> int:
    from pilot_workers.runners import get_runner

    if provider_key not in providers.PROVIDERS:
        print(f"error: unknown provider: {provider_key} "
              f"(available: {', '.join(sorted(providers.PROVIDERS))})",
              file=sys.stderr)
        return 2
    provider = providers.PROVIDERS[provider_key]
    path = get_runner(provider.runner).credential_path(provider)
    if not path.exists():
        print(f"  no API key recorded for {provider_key}")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"  removed the {provider_key} API key ({path})")
    return 0


# ------------------------------------------------------------------
# entry points
# ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """pw9 install ..."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(INSTALL_USAGE, end="")
        return 0
    if args[0] == "runner":
        if len(args) != 2:
            print(INSTALL_USAGE, end="", file=sys.stderr)
            return 2
        return _install_runner(args[1])
    print(INSTALL_USAGE, end="", file=sys.stderr)
    return 2


def uninstall_main(argv: list[str] | None = None) -> int:
    """pw9 uninstall ..."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(UNINSTALL_USAGE, end="")
        return 0
    if args[0] == "runner":
        if len(args) != 2:
            print(UNINSTALL_USAGE, end="", file=sys.stderr)
            return 2
        return _uninstall_runner(args[1])
    if args[0] == "key":
        if len(args) != 2:
            print(UNINSTALL_USAGE, end="", file=sys.stderr)
            return 2
        return _uninstall_key(args[1])
    print(UNINSTALL_USAGE, end="", file=sys.stderr)
    return 2


def key_main(argv: list[str] | None = None) -> int:
    """pw9 key <provider>"""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(KEY_USAGE, end="")
        return 0
    if len(args) != 1:
        print(KEY_USAGE, end="", file=sys.stderr)
        return 2
    return _key_configure(args[0])
