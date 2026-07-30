#!/usr/bin/env python3
"""Status reporting: provider credentials and runner presence.

    pw9 status [--json]
"""

from __future__ import annotations

import json
import sys

from pilot_workers import providers, runtime
from pilot_workers.runners import RUNNERS, get_runner

STATUS_USAGE = "usage: pw9 status [--json]"


def _collect() -> dict:
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
            version = runner.probe_version(binary)
        runners_info[name] = {
            "present": present,
            "version": version,
            "pinned": runner.pinned_version,
            "binary": str(binary) if binary else None,
        }

    return {
        "providers": providers_info,
        "runners": runners_info,
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
        [key, "ok" if info["credential"]["configured"] else "missing"]
        for key, info in data["providers"].items()
    ]
    lines += _render_table(["PROVIDER", "CREDENTIAL"], rows)
    for key, info in data["providers"].items():
        parts = []
        if info.get("strengths"):
            parts.append(info["strengths"])
        if info.get("suitable_modes"):
            parts.append(f"modes: {info['suitable_modes']}")
        if info.get("notes"):
            parts.append(info["notes"])
        if parts:
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
                f"run: pw9 install runner {name})"
            )
        rows.append([name, "yes" if info["present"] else "no", version])
    lines += _render_table(["RUNNER", "PRESENT", "VERSION"], rows)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(STATUS_USAGE)
        return 0
    json_mode = "--json" in args
    args = [a for a in args if a != "--json"]
    if args:
        print(STATUS_USAGE, file=sys.stderr)
        return 2
    try:
        data = _collect()
        if json_mode:
            print(json.dumps(data, indent=2))
        else:
            print(_render_human(data))
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
