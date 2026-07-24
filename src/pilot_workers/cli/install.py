#!/usr/bin/env python3
"""Install integration files (host playbook skill) to host config directories.

Grammar (v0.5.0, design D2):

    pilot-workers install <host|all> [--target <dir>]
    pilot-workers install runner <name>
    pilot-workers uninstall <host|all>
    pilot-workers uninstall runner <name>

The provider dimension and the ``on`` keyword are gone; removed forms are
usage errors (exit 2) with no deprecation notes. ``install_host(host, target)``
copies ``INTEGRATIONS_DIR/<host>-host/skills/pilot-workers/`` recursively into
the host's skill directory (claude: ``~/.claude/skills``; codex:
``$CODEX_HOME/skills``; either overridden by ``--target``).

The install manifest (schema v3) lives at <pilot_home>/install-manifest.json:

    {"schema_version": 3,
     "installs": {"<host>": {"installed_at": ..., "package_version": ...,
                             "files": [...], "created_dirs": [...]}}}

Installing over a v1/v2 manifest purges every legacy entry for the host (v1
``__all__`` first, then each v2 provider entry) — one printed line per removed
file — and the on-disk file is rewritten as a clean v3 via ``os.replace``.
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

MANIFEST_SCHEMA_VERSION = 3

HOSTS = ("claude", "codex")

# The leading line is the authoritative v0.5.0 grammar. The trailing note keeps
# ``install --help`` honest about the removed matrix form (``<provider> on
# <host|all>``); it is documentation, not a deprecation/alias: the parser
# rejects that form with a usage error.
INSTALL_USAGE = (
    "usage: pilot-workers install <host|all> [--target <dir>]\n"
    "       pilot-workers install runner <name>\n"
    "\n"
    "v0.5.0 host-level grammar; '<provider> on <host|all>' is no longer accepted."
)

UNINSTALL_USAGE = (
    "usage: pilot-workers uninstall <host|all>\n"
    "       pilot-workers uninstall runner <name>\n"
    "\n"
    "v0.5.0 host-level grammar; '<provider> on <host|all>' is no longer accepted."
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
        # In-memory v1 → v3 migration: a v1 host-level entry becomes a legacy
        # "__all__" sub-entry so the install path can purge it uniformly. The
        # file itself is rewritten as clean v3 on the next install.
        data = {
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


def _purge_entry(entry: dict) -> None:
    """Remove every file/dir recorded by a previous install entry.

    Prints one ``removed:`` line per file (and per emptied created dir) so a
    reinstall or migration leaves an auditable trail. Missing files are skipped
    silently. Directories are removed deepest-first so nested dirs go before
    their parents; only ``created_dirs`` entries are touched (never file parents
    the user may own).
    """
    for name in entry.get("files", []):
        try:
            os.unlink(name)
        except OSError:
            continue
        print(f"  removed: {name}")
    candidates = {Path(d) for d in entry.get("created_dirs", [])}
    for directory in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue
        print(f"  removed: {directory}")


def _host_purge_entries(host_entry) -> list[dict]:
    """Ordered list of legacy/previous entry dicts to purge for one host.

    A v3 flat entry (top-level ``files`` list, i.e. a reinstall) is returned as
    a single entry. A legacy v1/v2 nesting is returned as: the ``__all__``
    entry first (v1 host-level bundle), then every remaining provider sub-entry
    (v2 per-provider installs), so D2's purge ordering is preserved.
    """
    if not isinstance(host_entry, dict) or not host_entry:
        return []
    if isinstance(host_entry.get("files"), list):
        return [host_entry]
    ordered: list[dict] = []
    all_entry = host_entry.get("__all__")
    if isinstance(all_entry, dict):
        ordered.append(all_entry)
    for key in sorted(host_entry):
        if key == "__all__":
            continue
        value = host_entry[key]
        if isinstance(value, dict):
            ordered.append(value)
    return ordered


# ----------------------------------------------------------------------
# asset installer (host-level playbook skill)
# ----------------------------------------------------------------------


def install_host(host: str, target: Path | None = None) -> dict:
    """Copy the host's pilot-workers skill tree into its skill directory.

    Returns ``{"files": [...], "created_dirs": [...]}``. ``target`` overrides
    the host's default base (claude: ``~/.claude``; codex: ``$CODEX_HOME/
    skills``) so isolation tests never touch the real host config dirs. When
    ``target`` is given it IS the base: claude files land under
    ``<target>/skills/pilot-workers/``, codex under ``<target>/pilot-workers/``.
    """
    if host == "claude":
        base = (target or Path.home() / ".claude").resolve()
        dst = base / "skills" / "pilot-workers"
    elif host == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        base = (target or codex_home / "skills").resolve()
        dst = base / "pilot-workers"
    else:
        raise RuntimeError(f"unknown host: {host}")

    src = INTEGRATIONS_DIR / f"{host}-host" / "skills" / "pilot-workers"
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")

    files: list[str] = []
    created_dirs: list[str] = []

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
        new_parents: list[str] = []
        p = parent
        while not p.exists() and p != dst:
            new_parents.append(str(p))
            p = p.parent
        parent.mkdir(parents=True, exist_ok=True)
        created_dirs.extend(new_parents)
        if dest_file.is_symlink() or dest_file.exists():
            dest_file.unlink()
        shutil.copy2(src_file, dest_file)
        files.append(str(dest_file))
    print(f"  installed skill: {host}/pilot-workers/")
    return {"files": files, "created_dirs": created_dirs}


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
    """Parse post-subcommand argv into an action spec (raises _UsageError).

    Host form:  ``{"host": <host|all>, "target": <Path|None>}``
    Runner form: ``{"kind": "runner", "name": <name>, "target": <Path|None>}``
    Help form:  ``{"kind": "help"}``
    """
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

    if len(args) == 1 and args[0] in (*HOSTS, "all"):
        return {"host": args[0], "target": target}

    raise _UsageError(usage)


# ----------------------------------------------------------------------
# runner branch
# ----------------------------------------------------------------------


def _install_runner(name: str) -> int:
    from pilot_workers.runners import RUNNERS
    from pilot_workers.runners.opencode_runner import (
        PINNED_OPENCODE_VERSION,
        clear_version_cache,
    )

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
    # D6: a fresh install invalidates any cached --version result.
    clear_version_cache()
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
    if spec.get("kind") == "help":
        print(INSTALL_USAGE)
        return 0
    if spec.get("kind") == "runner":
        if spec.get("target"):
            print("error: --target is not supported for runner installs",
                  file=sys.stderr)
            return 2
        return _install_runner(spec["name"])

    hosts = list(HOSTS) if spec["host"] == "all" else [spec["host"]]
    try:
        manifest_path = _manifest_path()
        manifest = _load_manifest(manifest_path)
        installs = manifest.setdefault("installs", {})
        for host in hosts:
            # Migration / reinstall: purge every previous entry for this host
            # (v3 flat reinstall OR legacy v1 __all__ + v2 providers), then
            # write a clean host-level v3 entry.
            for entry in _host_purge_entries(installs.get(host)):
                _purge_entry(entry)
            print(f"Installing {host} integrations...")
            result = install_host(host, spec["target"])
            installs[host] = {
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "package_version": _package_version(),
                "files": result["files"],
                "created_dirs": result["created_dirs"],
            }
            # Write after EACH host: a crash mid-``install all`` must not lose
            # the hosts that already completed, and the os.replace inside
            # _write_manifest guarantees no v1/v2 file survives to be
            # re-migrated.
            _write_manifest(manifest_path, manifest)
        print("Done.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ----------------------------------------------------------------------
# uninstall
# ----------------------------------------------------------------------


def uninstall_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _parse_grammar(argv, "uninstall", UNINSTALL_USAGE)
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if spec.get("kind") == "help":
        print(UNINSTALL_USAGE)
        return 0
    if spec.get("target"):
        print("error: --target is not supported for uninstall", file=sys.stderr)
        return 2
    if spec.get("kind") == "runner":
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

    purged_any = False
    try:
        for host in hosts:
            host_entry = installs.get(host)
            if not host_entry:
                continue
            purged_any = True
            print(f"Uninstalling {host} integrations...")
            # Works on v3 flat installs AND legacy v1/v2 nestings: purge every
            # recorded entry for the host.
            for entry in _host_purge_entries(host_entry):
                _purge_entry(entry)
            del installs[host]
        if not purged_any:
            print(f"error: no manifest entry for: {', '.join(hosts)}",
                  file=sys.stderr)
            return 1
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
