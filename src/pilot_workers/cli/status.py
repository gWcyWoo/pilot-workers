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
import sys

from pilot_workers import providers, runtime
from pilot_workers.cli import install as install_mod
from pilot_workers.runners import RUNNERS, get_runner

STATUS_USAGE = (
    "usage: pilot-workers status [--json]\n"
    "       pilot-workers status <host> [--json]"
)


def _host_issues(host: str, provider_keys: list[str],
                 defaults: dict[str, str], entry) -> list[str]:
    """Divergence between what the manifest records and what is on disk /
    in the registry. Each check contributes one human-readable string; a
    healthy install yields []. Must never raise: a divergence report that
    crashes the command hides the very drift it should name."""
    issues: list[str] = []
    if provider_keys:
        skill_path = install_mod._deployed_skill_path(entry)
        missing = skill_path is None
        if not missing:
            try:
                missing = not skill_path.is_file()
            except OSError:
                missing = True
        if missing:
            issues.append(
                f"{host}: providers are recorded but the deployed skill "
                "is missing on disk"
            )
    else:
        # The other direction: the skill is written before the manifest commits,
        # so a crash in between leaves the planner reading a doctrine that
        # advertises providers this host no longer records. Only the
        # recorded-but-missing case used to be reported, which read as "nothing
        # is configured here" while the host went on delegating.
        try:
            # Prefer the RECORDED path: a `--target` deployment does not live at
            # the default location, and checking only the default reported a
            # custom-path orphan as a clean machine.
            orphan = install_mod._deployed_skill_path(entry)
            if orphan is None:
                orphan = install_mod.install_host_destination(host, None) / "SKILL.md"
            if orphan.is_file():
                issues.append(
                    f"{host}: a deployed skill exists at {orphan} but no "
                    "provider is recorded — it advertises workers this host no "
                    "longer has (an older deployment, or a crash between the "
                    "skill write and the manifest commit). Fix with "
                    f"'pilot-workers install <provider> on {host}' or "
                    f"'pilot-workers uninstall {host}'"
                )
        except (OSError, RuntimeError):
            pass
    for mode, provider in sorted(defaults.items()):
        if provider not in providers.PROVIDERS:
            issues.append(
                f"{host}: default for {mode} names provider {provider!r} "
                "which is not in the registry"
            )
        elif provider not in provider_keys:
            issues.append(
                f"{host}: default for {mode} names provider {provider!r} "
                "which is not in the host's providers"
            )
    for key in provider_keys:
        if key not in providers.PROVIDERS:
            issues.append(
                f"{host}: provider {key!r} is recorded but is not in "
                "the registry (its YAML may have been deleted)"
            )
    return issues


def _host_install_info(host: str, installs: dict) -> dict:
    """JSON/overview info for one host entry. A v3 flat entry has a top-level
    ``files`` list; anything else (absent, or a legacy v1/v2 nesting) reads as
    not-installed and should be (re)installed to migrate. Either way the
    recorded worker config (providers/defaults) and any divergence issues
    are reported."""
    entry = installs.get(host) if isinstance(installs, dict) else None
    provider_keys = install_mod.host_providers(installs, host)
    defaults = install_mod.host_modes(installs, host)
    info: dict = {
        "providers": provider_keys,
        "modes": defaults,
        "issues": _host_issues(host, provider_keys, defaults, entry),
    }
    if isinstance(entry, dict) and isinstance(entry.get("files"), list):
        info.update({
            "installed": True,
            "installed_at": entry.get("installed_at"),
            "package_version": entry.get("package_version"),
            "files": entry.get("files", []),
        })
    else:
        info["installed"] = False
    return info


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
            # Through the adapter: OpenCode's override shares the cached probe
            # with resolve_binary, another runner gets the plain `--version`.
            version = runner.probe_version(binary)
        runners_info[name] = {
            "present": present,
            "version": version,
            "pinned": runner.pinned_version,
            "binary": str(binary) if binary else None,
        }

    installs_info: dict = {}
    for host in install_mod.HOSTS:
        installs_info[host] = _host_install_info(host, installs)

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
    for host, info in data["installs"].items():
        provider_keys = info.get("providers", [])
        defaults = info.get("modes", {})
        if provider_keys:
            lines.append(f"  {host} providers: {', '.join(provider_keys)}")
        for mode, provider in sorted(defaults.items()):
            lines.append(f"  {host} default {mode}: {provider}")
        for issue in info.get("issues", []):
            lines.append(f"!! {issue}")
    if not any(info.get("providers") for info in data["installs"].values()):
        # Nothing configured anywhere: say what the first step is. `install`
        # prints a next-step line, so a fresh (or freshly wiped) machine should
        # not have to guess just because it asked `status` first.
        lines.append("")
        lines.append("No worker is configured for any host — this session does "
                     "every mode itself.")
        lines.append(f"  get started: pilot-workers install <provider> on "
                     f"{providers.HOSTS[0]} --global-key")
    return "\n".join(lines)


def _host_detail(host: str, json_mode: bool) -> int:
    manifest = install_mod._load_manifest(install_mod._manifest_path())
    installs = manifest.get("installs", {})
    entry = installs.get(host)
    info = _host_install_info(host, installs)
    if json_mode:
        # `providers`/`modes`/`issues` too: the human form prints every issue as
        # `!! ...` and the overview JSON carries them, so a machine consumer
        # using the per-host form to detect drift saw a clean host where the
        # human form would have shown a missing skill or an orphaned one.
        print(json.dumps({
            "host": host,
            "installed": info["installed"],
            "providers": info["providers"],
            "modes": info["modes"],
            "issues": info["issues"],
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
    provider_keys = info["providers"]
    defaults = info["modes"]
    if provider_keys:
        print(f"  providers: {', '.join(provider_keys)}")
    if defaults:
        print("  defaults:")
        for mode, provider in sorted(defaults.items()):
            print(f"    {mode}: {provider}")
    for issue in info["issues"]:
        print(f"!! {issue}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        # A question, not a mistake: stdout and exit 0.
        print(STATUS_USAGE)
        return 0
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
