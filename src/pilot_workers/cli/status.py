#!/usr/bin/env python3
"""Status reporting: provider credentials, host-level installs, runner presence.

    pilot-workers status [--json]
    pilot-workers status <host> [--json]

v0.5.0 (design D2): installs are reported per HOST (the provider dimension is
gone). The overview keeps per-provider credential status and adds a host-level
installs section + runner presence. ``status <host>`` is the detail form; the
old ``status <provider> on <host>`` pair form is a usage error (exit 2).
``status --json`` keys installs by host only.
"""

from __future__ import annotations

import json
import subprocess
import sys

from pilot_workers import providers, runtime
from pilot_workers.cli import install as install_mod
from pilot_workers.runners import RUNNERS, get_runner, opencode_runner
from pilot_workers.runners.opencode_runner import PINNED_OPENCODE_VERSION

STATUS_USAGE = (
    "usage: pilot-workers status [--json]\n"
    "       pilot-workers status <host> [--json]"
)


def _host_install_info(entry) -> dict:
    """JSON/overview info for one host entry. A v3 flat entry has a top-level
    ``files`` list; anything else (absent, or a legacy v1/v2 nesting) reads as
    not-installed and should be (re)installed to migrate."""
    if isinstance(entry, dict) and isinstance(entry.get("files"), list):
        return {
            "installed": True,
            "installed_at": entry.get("installed_at"),
            "package_version": entry.get("package_version"),
            "files": entry.get("files", []),
        }
    return {"installed": False}


def _collect() -> dict:
    manifest = install_mod._load_manifest(install_mod._manifest_path())
    installs = manifest.get("installs", {})

    providers_info: dict = {}
    for key in sorted(providers.PROVIDERS):
        provider = providers.PROVIDERS[key]
        runner = get_runner(provider.runner)
        credential = runtime.credential_metadata(provider, runner)
        providers_info[key] = {
            "credential": {
                "configured": credential["configured"],
                "path": credential["path"],
            },
            "strengths": provider.strengths,
            "suitable_modes": provider.suitable_modes,
            "notes": provider.notes,
        }

    runners_info: dict = {}
    for name in sorted(RUNNERS):
        runner = get_runner(name)
        binary_path = getattr(runner, "binary_path", None)
        binary = binary_path() if callable(binary_path) else None
        present = bool(binary and binary.is_file())
        version: str | None = None
        if present:
            if name == "opencode":
                # D6: share the cached --version seam with resolve_binary.
                version = opencode_runner.probe_version(binary)
            else:
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

    installs_info: dict = {}
    for host in install_mod.HOSTS:
        installs_info[host] = _host_install_info(installs.get(host))

    return {
        "providers": providers_info,
        "runners": runners_info,
        "installs": installs_info,
    }


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
        ]
        for key, info in data["providers"].items()
    ]
    lines += _render_table(["PROVIDER", "CREDENTIAL"], rows)
    for key, info in data["providers"].items():
        strengths = info.get("strengths", "")
        suitable_modes = info.get("suitable_modes", "")
        note = info.get("notes", "")
        if strengths or suitable_modes or note:
            parts = []
            if strengths:
                parts.append(strengths)
            if suitable_modes:
                parts.append(f"modes: {suitable_modes}")
            if note:
                parts.append(note)
            lines.append(f"  {key}: {' — '.join(parts)}")
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
    lines.append("")
    lines.append("Installs")
    rows = []
    for host, info in data["installs"].items():
        if info.get("installed"):
            rows.append([host, "yes", str(len(info.get("files", [])))])
        else:
            rows.append([host, "no", "0"])
    lines += _render_table(["HOST", "INSTALLED", "FILES"], rows)
    return "\n".join(lines)


def _host_detail(host: str, json_mode: bool) -> int:
    manifest = install_mod._load_manifest(install_mod._manifest_path())
    entry = manifest.get("installs", {}).get(host)
    info = _host_install_info(entry)
    if json_mode:
        print(json.dumps({
            "host": host,
            "installed": info["installed"],
            "entry": entry if info["installed"] else None,
        }, indent=2))
        return 0
    if not info["installed"]:
        print(f"{host}: not installed")
        return 0
    print(f"{host}: installed")
    print(f"  installed_at: {entry.get('installed_at', '-')}")
    print(f"  package_version: {entry.get('package_version', '-')}")
    print(f"  files ({len(entry.get('files', []))}):")
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

        if len(args) == 1 and args[0] in install_mod.HOSTS:
            return _host_detail(args[0], json_mode)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(STATUS_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
