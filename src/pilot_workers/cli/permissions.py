#!/usr/bin/env python3
"""User-level permission overrides: add/remove/show per provider and mode.

`permissions add ds explore "apifox *"` widens one worker's sandbox on THIS
machine only. The rule lands in ``$PILOT_WORKERS_HOME/permissions/<provider>
.json`` — user configuration, so it survives pip upgrades — and
``policy.effective_permissions`` merges it as the third, highest layer:
built-in mode defaults, then the provider's named profile, then these
overrides. Every merge re-pins the redirect (``*>*``) and credential-path
denies and the file-tool floor, so an allow for a NEW pattern cannot displace
them; naming a guardrail pattern itself is a deliberate override — honoured,
but the CLI warns.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from pilot_workers import policy
from pilot_workers.providers import PROVIDERS

USAGE = """usage:
  pilot-workers permissions add <provider> <mode|_all> <pattern> [--action allow|deny]
  pilot-workers permissions remove <provider> <mode|_all> <pattern>
  pilot-workers permissions show <provider> [<mode>]

Patterns are OpenCode shell-command globs (last-match-wins). Rules are stored
per provider in $PILOT_WORKERS_HOME/permissions/<provider>.json and applied on
top of the provider's profile at every dispatch — no reinstall needed. `_all`
applies the rule to every mode. Redirect (`*>*`) and credential-path denies
stay pinned last; an allow for a new pattern cannot displace them, and naming
a guardrail pattern itself triggers a warning.
"""

_ACTIONS = ("allow", "deny")
_SECTIONS = policy.VALID_MODES + ("_all",)


def _write_overrides(provider_key: str, data: dict[str, Any]) -> None:
    from pilot_workers import runtime

    path = policy.permission_overrides_path(provider_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.atomic_write_text(
        path, json.dumps(data, indent=2) + "\n", mode=0o600,
        prefix=".permissions.")


def _add(provider_key: str, section: str, pattern: str, action: str) -> int:
    if provider_key not in PROVIDERS:
        print(f"error: unknown provider: {provider_key} "
              f"(expected one of {', '.join(sorted(PROVIDERS))})",
              file=sys.stderr)
        return 2
    if section not in _SECTIONS:
        print(f"error: unknown mode: {section} "
              f"(expected _all or one of {', '.join(policy.VALID_MODES)})",
              file=sys.stderr)
        return 2
    if action not in _ACTIONS:
        print(f"error: unknown action: {action} (expected allow or deny)",
              file=sys.stderr)
        return 2
    try:
        data = policy.load_permission_overrides(provider_key) or {}
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if action == "allow" and pattern in (*policy.CREDENTIAL_PATH_DENIES, "*>*"):
        # Naming a pinned guardrail is a deliberate override the merge honours
        # (it re-pins only patterns whose effective value is still deny). Legal,
        # but never silent.
        print(f"  warning: \"{pattern}\" is a pinned guardrail; this allow "
              f"WILL displace it for {provider_key}", file=sys.stderr)
    shell = data.setdefault(section, {}).setdefault("shell", {})
    previous = shell.get(pattern)
    # Re-insert so the rule moves to the END of its section: last-match-wins,
    # and the operator's latest word should be the one that wins.
    shell.pop(pattern, None)
    shell[pattern] = action
    _write_overrides(provider_key, data)
    if previous == action:
        print(f"  already present: \"{pattern}\" -> {action} "
              f"({provider_key}, {section})")
    else:
        print(f"  added: \"{pattern}\" -> {action} ({provider_key}, {section})")
    print(f"  applies to every new dispatch; stored at "
          f"{policy.permission_overrides_path(provider_key)}")
    return 0


def _remove(provider_key: str, section: str, pattern: str) -> int:
    # Removal is permissive on purpose (mirrors uninstall): the provider may
    # have left the registry, or the mode the rule names may no longer exist —
    # cleaning the stored rule must still work.
    try:
        data = policy.load_permission_overrides(provider_key)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    shell = (data or {}).get(section, {}).get("shell", {})
    if not data or pattern not in shell:
        print(f"note: no rule \"{pattern}\" for {provider_key}/{section}")
        return 0
    del shell[pattern]
    if not shell:
        data[section].pop("shell", None)
    if not data[section]:
        del data[section]
    path = policy.permission_overrides_path(provider_key)
    if data:
        _write_overrides(provider_key, data)
    else:
        path.unlink()
        print(f"  removed last rule; deleted {path}")
        return 0
    print(f"  removed: \"{pattern}\" ({provider_key}, {section})")
    return 0


def _origin(pattern: str, mode: str,
            profile: dict[str, Any] | None,
            overrides: dict[str, Any] | None) -> str:
    """Which layer last touched this shell pattern, for display only."""
    for source, tag in ((overrides, "override"), (profile, "profile")):
        if not source:
            continue
        for section in (mode, "_all"):
            if pattern in (source.get(section, {}) or {}).get("shell", {}):
                return f"  [{tag}]"
    return ""


def _show(provider_key: str, mode: str | None) -> int:
    provider = PROVIDERS.get(provider_key)
    if provider is None:
        print(f"error: unknown provider: {provider_key} "
              f"(expected one of {', '.join(sorted(PROVIDERS))})",
              file=sys.stderr)
        return 2
    if mode is not None and mode not in policy.VALID_MODES:
        print(f"error: unknown mode: {mode} "
              f"(expected one of {', '.join(policy.VALID_MODES)})",
              file=sys.stderr)
        return 2
    profile_name = provider.permissions
    try:
        profile = (policy.load_permission_profile(profile_name)
                   if profile_name else None)
        overrides = policy.load_permission_overrides(provider_key)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    overrides_path = policy.permission_overrides_path(provider_key)
    print(f"{provider_key}: profile={profile_name or '-'} "
          f"overrides={overrides_path if overrides else '-'}")
    for m in ([mode] if mode else policy.VALID_MODES):
        effective = policy.effective_permissions(provider, m)
        print(f"\n== {m}" + (" (resume uses code)" if m == "code" else ""))
        bash = effective["bash"]
        if isinstance(bash, dict):
            print("  shell (last match wins):")
            for pattern, action in bash.items():
                print(f"    {json.dumps(pattern)} {action}"
                      f"{_origin(pattern, m, profile, overrides)}")
        else:
            # A layer set bash wholesale (`tools: {bash: deny}`); render the
            # scalar rather than crashing the inspection command.
            print(f"  shell: {bash}")
        print("  tools:")
        for tool, value in effective.items():
            if tool == "bash":
                continue
            rendered = ("allow (credential paths denied)"
                        if isinstance(value, dict) else value)
            print(f"    {tool}: {rendered}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    verb, rest = args[0], args[1:]

    if verb == "add":
        action = "allow"
        positional: list[str] = []
        i = 0
        while i < len(rest):
            if rest[i] == "--action":
                if i + 1 >= len(rest):
                    print("error: --action requires a value", file=sys.stderr)
                    return 2
                action = rest[i + 1]
                i += 2
            elif rest[i].startswith("--action="):
                action = rest[i].split("=", 1)[1]
                i += 1
            else:
                positional.append(rest[i])
                i += 1
        if len(positional) != 3:
            print(USAGE, end="", file=sys.stderr)
            return 2
        return _add(positional[0], positional[1], positional[2], action)

    if verb == "remove":
        if len(rest) != 3:
            print(USAGE, end="", file=sys.stderr)
            return 2
        return _remove(rest[0], rest[1], rest[2])

    if verb == "show":
        if len(rest) not in (1, 2):
            print(USAGE, end="", file=sys.stderr)
            return 2
        return _show(rest[0], rest[1] if len(rest) == 2 else None)

    print(f"error: unknown permissions verb: {verb}", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
