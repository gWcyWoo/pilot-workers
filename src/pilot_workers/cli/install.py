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


def _oauth_login(provider) -> int:
    """Hand an oauth provider's login to the engine that owns the flow.

    pw9 implements no OAuth: it only points the engine's XDG dirs at this
    provider's isolated profile and lets the engine run its own device-code
    exchange. The engine then writes the token payload to exactly the path
    pw9 already treats as this provider's credential file, and refreshes it
    there for the life of the grant.
    """
    import os

    from pilot_workers import runtime
    from pilot_workers.runners import get_runner

    runner = get_runner(provider.runner)
    binary = runner.binary_path()
    if binary is None or not binary.is_file():
        print(f"error: runner {provider.runner} is not installed; "
              f"run: pw9 install runner {provider.runner}", file=sys.stderr)
        return 1

    paths = providers.profile_paths(provider)
    for name in ("root", "config", "data", "state", "cache"):
        runtime.ensure_private_directory(paths[name])

    # The parent environment is inherited on purpose: this is an INTERACTIVE
    # command the user is running at their own terminal, and the login flow
    # needs TERM/tty to print its device code. Only the XDG dirs are
    # overridden, so the token lands in this provider's profile and nowhere
    # else. (Dispatch keeps its SAFE_ENV_KEYS rebuild — see build_environment.)
    env = dict(os.environ)
    env.update({
        "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "XDG_STATE_HOME": str(paths["state"]),
        "XDG_CACHE_HOME": str(paths["cache"]),
    })

    command = [str(binary), "auth", "login",
               "-p", provider.provider_id,
               "-m", provider.auth_method]
    print(f"  delegating login to {provider.runner}: "
          f"{provider.provider_id} / {provider.auth_method}")
    rc = subprocess.run(command, env=env).returncode
    if rc != 0:
        print(f"error: login failed (exit {rc})", file=sys.stderr)
        return rc

    path = runner.credential_path(provider)
    if not path.is_file():
        print(f"error: login reported success but wrote no credential at {path}",
              file=sys.stderr)
        return 1
    # The engine writes with its own umask; pw9's dispatch path refuses a
    # credential file readable by anyone else, so tighten it here rather than
    # let the next dispatch fail on a file this command produced.
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"warning: could not tighten {path}: {exc}", file=sys.stderr)
    print(f"  configured: {provider.key}")
    return 0


def _key_configure(provider_key: str) -> int:
    from pilot_workers import credentials

    if provider_key not in providers.PROVIDERS:
        print(f"error: unknown provider: {provider_key} "
              f"(available: {', '.join(sorted(providers.PROVIDERS))})",
              file=sys.stderr)
        return 2
    provider = providers.PROVIDERS[provider_key]
    if provider.auth == "oauth":
        try:
            return _oauth_login(provider)
        except (RuntimeError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
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
