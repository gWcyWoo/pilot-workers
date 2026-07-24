#!/usr/bin/env python3
"""Install integration files (agents, commands, skills) to host config directories.

Grammar (parsed on raw argv before any argparse-style handling):

    pilot-workers install <provider|all> on <host|all> [--target <dir>]
    pilot-workers install runner <name>
    pilot-workers uninstall <provider|all> on <host|all>
    pilot-workers uninstall runner <name>

Deprecated alias: 'install <host>' maps to 'install all on <host>'.

The install manifest (schema v2) lives at <pilot_home>/install-manifest.json:

    {"schema_version": 2,
     "installs": {"<host>": {"<provider>": {"installed_at": ...,
                                            "package_version": ...,
                                            "files": [...],
                                            "created_dirs": [...]}}}}

v1 manifests ({"hosts": {...}}) are migrated in memory on load: each host
entry becomes a legacy "__all__" entry under "installs".
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pilot_workers import providers


INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "integrations"

MANIFEST_SCHEMA_VERSION = 2

HOSTS = ("claude", "codex")

INSTALL_USAGE = (
    "usage: pilot-workers install <provider|all> on <host|all> [--target <dir>]\n"
    "       pilot-workers install runner <name>"
)

UNINSTALL_USAGE = (
    "usage: pilot-workers uninstall <provider|all> on <host|all>\n"
    "       pilot-workers uninstall runner <name>"
)


class _UsageError(Exception):
    pass


# ----------------------------------------------------------------------
# manifest
# ----------------------------------------------------------------------


def _manifest_path() -> Path:
    return providers.pilot_home() / "install-manifest.json"


def _package_version() -> str:
    try:
        return importlib.metadata.version("pilot-workers")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "installs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt install manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"corrupt install manifest {path}: expected JSON object")
    if "installs" not in data and "hosts" in data:
        # In-memory v1 → v2 migration: host-level entries become legacy
        # "__all__" entries; the file itself is only rewritten on install.
        data = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "installs": {
                host: {"__all__": entry}
                for host, entry in data.get("hosts", {}).items()
            },
        }
    data["schema_version"] = MANIFEST_SCHEMA_VERSION
    data.setdefault("installs", {})
    return data


def _write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".install-manifest.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(data, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _purge_entry(entry: dict, label: str) -> None:
    """Remove files recorded by a previous install entry (same host/provider)."""
    removed = 0
    for name in entry.get("files", []):
        try:
            os.unlink(name)
            removed += 1
        except OSError:
            pass
    # Drop directories the previous install created, if now empty
    # (deepest first, so nested dirs like dispatch/references/ go before dispatch/).
    # Only use created_dirs — do not try to rmdir file parents that the user may own.
    candidates = {Path(d) for d in entry.get("created_dirs", [])}
    for directory in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    print(f"  removed {removed} stale file(s) from previous {label}")


# ----------------------------------------------------------------------
# asset installers (per provider, per host)
# ----------------------------------------------------------------------


def install_claude(provider: providers.Provider, target: Path | None = None) -> dict:
    """Copy this provider's Claude Code agent and command definitions."""
    base = (target or Path.home() / ".claude").resolve()
    src = INTEGRATIONS_DIR / "claude-host"
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")
    prefix = provider.asset_prefix

    files: list[str] = []
    created_dirs: list[str] = []

    def _mkdir(path: Path) -> None:
        p = path
        new_parents = []
        while not p.exists():
            new_parents.append(str(p))
            p = p.parent
        path.mkdir(parents=True, exist_ok=True)
        created_dirs.extend(new_parents)

    agents_src = src / "agents"
    if agents_src.is_dir():
        agents_dst = base / "agents"
        _mkdir(agents_dst)
        for f in sorted(agents_src.glob(f"{prefix}-*.md")):
            shutil.copy2(f, agents_dst / f.name)
            files.append(str(agents_dst / f.name))
            print(f"  installed agent: {f.name}")

    # The commands dir may not exist for a provider (e.g. ds) — that is fine.
    commands_src = src / "commands" / prefix
    if commands_src.is_dir():
        dst = base / "commands" / prefix
        _mkdir(dst)
        for f in sorted(commands_src.glob("*.md")):
            shutil.copy2(f, dst / f.name)
            files.append(str(dst / f.name))
            print(f"  installed command: {prefix}/{f.name}")

    return {"files": files, "created_dirs": created_dirs}


def install_codex(provider: providers.Provider, target: Path | None = None) -> dict:
    """Copy this provider's Codex skill definition."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    base = (target or codex_home / "skills").resolve()
    prefix = provider.asset_prefix
    src = INTEGRATIONS_DIR / "codex-host" / prefix
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")

    files: list[str] = []
    created_dirs: list[str] = []

    dst = base / prefix
    existed_before = dst.exists()
    dst.mkdir(parents=True, exist_ok=True)
    if not existed_before:
        created_dirs.append(str(dst))
    for src_file in sorted(src.rglob("*")):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dest_file = dst / rel
        parent = dest_file.parent
        new_parents = []
        p = parent
        while not p.exists() and p != dst:
            new_parents.append(str(p))
            p = p.parent
        parent.mkdir(parents=True, exist_ok=True)
        created_dirs.extend(new_parents)
        shutil.copy2(src_file, dest_file)
        files.append(str(dest_file))
    print(f"  installed skill: {prefix}/")

    return {"files": files, "created_dirs": created_dirs}


def _install_pair(provider: providers.Provider, host: str, target: Path | None) -> dict:
    if host == "claude":
        return install_claude(provider, target)
    return install_codex(provider, target)


# ----------------------------------------------------------------------
# grammar (raw argv, before argparse-style handling)
# ----------------------------------------------------------------------


def _strip_target(argv: list[str]) -> tuple[list[str], Path | None]:
    args: list[str] = []
    target: Path | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--target":
            if i + 1 >= len(argv):
                raise _UsageError("--target requires a directory argument")
            target = Path(argv[i + 1])
            i += 2
        elif arg.startswith("--target="):
            target = Path(arg.split("=", 1)[1])
            i += 1
        else:
            args.append(arg)
            i += 1
    return args, target


def _parse_grammar(argv: list[str], command: str, usage: str) -> dict:
    """Parse post-subcommand argv into an action spec (raises _UsageError)."""
    if argv and argv[0] in ("-h", "--help"):
        return {"kind": "help"}
    args, target = _strip_target(argv)

    if args and args[0] == "runner":
        if len(args) == 2:
            from pilot_workers.runners import RUNNERS

            name = args[1]
            if name not in RUNNERS:
                raise _UsageError(
                    f"unknown runner: {name} "
                    f"(available: {', '.join(sorted(RUNNERS))})"
                )
            return {"kind": "runner", "name": name, "target": target}
        raise _UsageError(f"usage: pilot-workers {command} runner <name>")

    if len(args) == 3 and args[1] == "on":
        provider_key, host = args[0], args[2]
        if provider_key != "all" and provider_key not in providers.PROVIDERS:
            raise _UsageError(
                f"unknown provider: {provider_key} "
                f"(available: {', '.join(sorted(providers.PROVIDERS))}, all)"
            )
        if host != "all" and host not in HOSTS:
            raise _UsageError(
                f"unknown host: {host} (available: {', '.join(HOSTS)}, all)"
            )
        return {
            "kind": "matrix",
            "provider": provider_key,
            "host": host,
            "target": target,
        }

    if len(args) == 1 and args[0] in (*HOSTS, "all"):
        host = args[0]
        print(
            f"note: '{command} {host}' is deprecated; "
            f"use 'pilot-workers {command} all on {host}'",
            file=sys.stderr,
        )
        return {
            "kind": "matrix",
            "provider": "all",
            "host": host,
            "target": target,
        }

    message = usage
    if len(args) == 2 and args[1] in (*HOSTS, "all"):
        message += f"\ndid you mean '{command} {args[0]} on {args[1]}'?"
    raise _UsageError(message)


# ----------------------------------------------------------------------
# runner branch
# ----------------------------------------------------------------------


def _install_runner(name: str) -> int:
    from pilot_workers.runners import RUNNERS
    from pilot_workers.runners.opencode_runner import PINNED_OPENCODE_VERSION

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    import pilot_workers

    script = (
        Path(pilot_workers.__file__).resolve().parent
        / "scripts"
        / "install_runtime.sh"
    )
    rc = subprocess.run(["bash", str(script)]).returncode
    if rc != 0:
        return rc
    runtime_root = providers.pilot_home() / "worker-runtime" / "opencode"
    if runtime_root.is_dir():
        for child in sorted(runtime_root.iterdir()):
            if child.name != PINNED_OPENCODE_VERSION:
                print(f"note: stale runner version present: {child}")
    return 0


def _uninstall_runner(name: str) -> int:
    from pilot_workers.runners import RUNNERS

    if name not in RUNNERS:
        print(f"error: unknown runner: {name}", file=sys.stderr)
        return 2
    runtime_root = providers.pilot_home() / "worker-runtime" / "opencode"
    if not runtime_root.exists():
        print(f"note: no runner install found at {runtime_root}")
        return 0
    for child in sorted(runtime_root.iterdir()):
        print(f"removed: {child}")
    shutil.rmtree(runtime_root)
    print(f"removed: {runtime_root}")
    return 0


# ----------------------------------------------------------------------
# install
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _parse_grammar(argv, "install", INSTALL_USAGE)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if spec["kind"] == "help":
        print(INSTALL_USAGE)
        return 0
    if spec["kind"] == "runner":
        if spec.get("target"):
            print("error: --target is not supported for runner installs",
                  file=sys.stderr)
            return 2
        return _install_runner(spec["name"])

    provider_keys = (
        sorted(providers.PROVIDERS)
        if spec["provider"] == "all"
        else [spec["provider"]]
    )
    hosts = list(HOSTS) if spec["host"] == "all" else [spec["host"]]
    try:
        manifest_path = _manifest_path()
        manifest = _load_manifest(manifest_path)
        installs = manifest.setdefault("installs", {})
        for host in hosts:
            host_entries = installs.setdefault(host, {})
            legacy = host_entries.pop("__all__", None)
            if legacy is not None:
                print(f"note: replacing legacy v0.2.0 install on {host}")
                _purge_entry(legacy, f"legacy {host} install")
            for key in provider_keys:
                provider = providers.PROVIDERS[key]
                previous = host_entries.get(key)
                if previous is not None:
                    _purge_entry(previous, f"{host} install ({key})")
                print(f"Installing {provider.display_name} integrations for {host}...")
                result = _install_pair(provider, host, spec["target"])
                host_entries[key] = {
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "package_version": _package_version(),
                    "files": result["files"],
                    "created_dirs": result["created_dirs"],
                }
                # Write after EACH (provider, host) pair: a crash mid-matrix
                # must not lose the pairs that already completed.
                _write_manifest(manifest_path, manifest)
        print("Done.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def _uninstall_entry(entry: dict) -> None:
    for name in entry.get("files", []):
        path = Path(name)
        if not path.exists():
            print(f"note: already gone: {name}")
            continue
        path.unlink()
        print(f"removed: {name}")
    dirs = sorted(entry.get("created_dirs", []),
                  key=lambda p: len(Path(p).parts), reverse=True)
    for name in dirs:
        try:
            os.rmdir(name)
            print(f"removed: {name}")
        except OSError:
            print(f"note: kept non-empty directory: {name}")


def uninstall_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _parse_grammar(argv, "uninstall", UNINSTALL_USAGE)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if spec["kind"] == "help":
        print(UNINSTALL_USAGE)
        return 0
    if spec.get("target"):
        print("error: --target is not supported for uninstall", file=sys.stderr)
        return 2
    if spec["kind"] == "runner":
        return _uninstall_runner(spec["name"])

    hosts = list(HOSTS) if spec["host"] == "all" else [spec["host"]]
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        print(f"error: no install manifest found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    installs = manifest.get("installs", {})

    targets: list[tuple[str, str]] = []
    for host in hosts:
        if spec["provider"] == "all":
            # Every provider entry AND any legacy "__all__" entry.
            keys = sorted(installs.get(host, {}))
            if not keys:
                print(f"note: no manifest entry for {host}, skipping",
                      file=sys.stderr)
            targets.extend((host, key) for key in keys)
        else:
            p = spec["provider"]
            if p in installs.get(host, {}):
                targets.append((host, p))
            elif "__all__" in installs.get(host, {}):
                print(f"note: {p} on {host} was installed as a legacy v0.2.0 bundle; "
                      f"removing entire legacy entry", file=sys.stderr)
                targets.append((host, "__all__"))
            else:
                targets.append((host, p))

    present = [(h, k) for h, k in targets if k in installs.get(h, {})]
    missing = [(h, k) for h, k in targets if k not in installs.get(h, {})]
    if not present:
        wanted = ", ".join(f"{k} on {h}" for h, k in missing) or ", ".join(hosts)
        print(f"error: no manifest entry for: {wanted}", file=sys.stderr)
        return 1
    for h, k in missing:
        print(f"note: no manifest entry for {k} on {h}, skipping", file=sys.stderr)

    try:
        for host, key in present:
            print(f"Uninstalling {key} integrations from {host}...")
            _uninstall_entry(installs[host].pop(key))
        for host in list(installs):
            if not installs[host]:
                del installs[host]
        if installs:
            _write_manifest(manifest_path, manifest)
        else:
            manifest_path.unlink()
        print("Done.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
