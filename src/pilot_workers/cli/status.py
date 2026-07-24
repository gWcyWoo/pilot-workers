#!/usr/bin/env python3
"""Status reporting: provider credentials, host installs, runner presence.

    pilot-workers status [--json]
    pilot-workers status <provider> on <host> [--json]
"""

from __future__ import annotations

import json
import subprocess
import sys

from pilot_workers import providers, runtime
from pilot_workers.cli import install as install_mod
from pilot_workers.runners import RUNNERS, get_runner
from pilot_workers.runners.opencode_runner import PINNED_OPENCODE_VERSION

STATUS_USAGE = (
    "usage: pilot-workers status [--json]\n"
    "       pilot-workers status <provider> on <host> [--json]"
)


def _collect() -> dict:
    manifest = install_mod._load_manifest(install_mod._manifest_path())
    installs = manifest.get("installs", {})

    providers_info: dict = {}
    for key in sorted(providers.PROVIDERS):
        provider = providers.PROVIDERS[key]
        runner = get_runner(provider.runner)
        credential = runtime.credential_metadata(provider, runner)
        host_state: dict = {}
        for host in install_mod.HOSTS:
            entries = installs.get(host, {})
            if key in entries:
                host_state[host] = "installed"
            elif "__all__" in entries:
                host_state[host] = "installed (legacy)"
            else:
                host_state[host] = "-"
        providers_info[key] = {
            "credential": {
                "configured": credential["configured"],
                "path": credential["path"],
            },
            "hosts": host_state,
        }

    runners_info: dict = {}
    for name in sorted(RUNNERS):
        runner = get_runner(name)
        binary_path = getattr(runner, "binary_path", None)
        binary = binary_path() if callable(binary_path) else None
        present = bool(binary and binary.is_file())
        version: str | None = None
        if present:
            proc = subprocess.run(
                [str(binary), "--version"],
                text=True, capture_output=True, check=False,
            )
            version = (proc.stdout or proc.stderr).strip() or None
        runners_info[name] = {
            "present": present,
            "version": version,
            "pinned": PINNED_OPENCODE_VERSION if name == "opencode" else None,
            "binary": str(binary) if binary else None,
        }

    return {"providers": providers_info, "runners": runners_info}


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    ]
    for row in rows:
        lines.append(
            "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return lines


def _render_human(data: dict) -> str:
    lines = ["Providers"]
    rows = [
        [
            key,
            "ok" if info["credential"]["configured"] else "missing",
            info["hosts"]["claude"],
            info["hosts"]["codex"],
        ]
        for key, info in data["providers"].items()
    ]
    lines += _render_table(["PROVIDER", "CREDENTIAL", "CLAUDE", "CODEX"], rows)
    lines.append("")
    lines.append("Runners")
    rows = []
    for name, info in data["runners"].items():
        version = info["version"] or "-"
        pinned = info["pinned"]
        if info["version"] and pinned and info["version"] != pinned:
            version = (
                f"{info['version']} (pinned {pinned} — "
                f"run: pilot-workers install runner {name})"
            )
        rows.append([name, "yes" if info["present"] else "no", version])
    lines += _render_table(["RUNNER", "PRESENT", "VERSION"], rows)
    return "\n".join(lines)


def _pair_status(provider_key: str, host: str, json_mode: bool) -> int:
    if provider_key not in providers.PROVIDERS:
        print(f"error: unknown provider: {provider_key}", file=sys.stderr)
        return 2
    if host not in install_mod.HOSTS:
        print(f"error: unknown host: {host}", file=sys.stderr)
        return 2
    manifest = install_mod._load_manifest(install_mod._manifest_path())
    entries = manifest.get("installs", {}).get(host, {})
    entry = entries.get(provider_key)
    legacy = False
    if entry is None and "__all__" in entries:
        entry = entries["__all__"]
        legacy = True
    if json_mode:
        print(json.dumps({
            "provider": provider_key,
            "host": host,
            "installed": entry is not None,
            "legacy": legacy,
            "entry": entry,
        }, indent=2))
        return 0
    if entry is None:
        print(f"{provider_key} on {host}: not installed")
        return 0
    state = "installed (legacy)" if legacy else "installed"
    print(f"{provider_key} on {host}: {state}")
    print(f"  installed_at: {entry.get('installed_at', '-')}")
    print(f"  package_version: {entry.get('package_version', '-')}")
    print("  files:")
    for name in entry.get("files", []):
        print(f"    {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in args
    args = [a for a in args if a != "--json"]

    try:
        if not args:
            data = _collect()
            if json_mode:
                print(json.dumps(data, indent=2))
            else:
                print(_render_human(data))
            return 0

        if len(args) == 3 and args[1] == "on":
            return _pair_status(args[0], args[2], json_mode)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(STATUS_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
