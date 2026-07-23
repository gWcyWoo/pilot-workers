#!/usr/bin/env python3
"""Install integration files (agents, commands, skills) to host config directories."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pilot_workers import providers


INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "integrations"

MANIFEST_SCHEMA_VERSION = 1


def _manifest_path() -> Path:
    return providers.pilot_home() / "install-manifest.json"


def _package_version() -> str:
    try:
        return importlib.metadata.version("pilot-workers")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": MANIFEST_SCHEMA_VERSION, "hosts": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"corrupt install manifest {path}: {exc}") from exc


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


def _purge_previous(host: str, manifest: dict) -> None:
    """Remove files recorded by a previous install of the same host."""
    entry = manifest.get("hosts", {}).get(host)
    if not entry:
        return
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
    print(f"  removed {removed} stale file(s) from previous {host} install")


def install_claude(target: Path | None = None) -> dict:
    """Copy Claude Code agent and command definitions."""
    base = (target or Path.home() / ".claude").resolve()
    src = INTEGRATIONS_DIR / "claude-host"
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")

    files: list[str] = []
    created_dirs: list[str] = []

    def _mkdir(path: Path) -> None:
        if not path.exists():
            created_dirs.append(str(path))
        path.mkdir(parents=True, exist_ok=True)

    agents_src = src / "agents"
    commands_src = src / "commands"

    if agents_src.is_dir():
        agents_dst = base / "agents"
        _mkdir(agents_dst)
        for f in agents_src.glob("*.md"):
            shutil.copy2(f, agents_dst / f.name)
            files.append(str(agents_dst / f.name))
            print(f"  installed agent: {f.name}")

    if commands_src.is_dir():
        for provider_dir in commands_src.iterdir():
            if not provider_dir.is_dir():
                continue
            dst = base / "commands" / provider_dir.name
            _mkdir(dst)
            for f in provider_dir.glob("*.md"):
                shutil.copy2(f, dst / f.name)
                files.append(str(dst / f.name))
                print(f"  installed command: {provider_dir.name}/{f.name}")

    return {"files": files, "created_dirs": created_dirs}


def install_codex(target: Path | None = None) -> dict:
    """Copy Codex skill definitions."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    base = (target or codex_home / "skills").resolve()
    src = INTEGRATIONS_DIR / "codex-host"
    if not src.is_dir():
        raise RuntimeError(f"integration source not found: {src}")

    files: list[str] = []
    created_dirs: list[str] = []

    for skill_dir in src.iterdir():
        if not skill_dir.is_dir():
            continue
        dst = base / skill_dir.name
        dst.mkdir(parents=True, exist_ok=True)
        # Copy files individually (do not rmtree — that would delete user content)
        for src_file in sorted(skill_dir.rglob("*")):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(skill_dir)
            dest_file = dst / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)
        created_dirs.append(str(dst))
        for f in sorted(dst.rglob("*")):
            if f.is_dir():
                created_dirs.append(str(f))
            elif f.is_file():
                files.append(str(f))
        print(f"  installed skill: {skill_dir.name}/")

    return {"files": files, "created_dirs": created_dirs}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install pilot-workers integrations.")
    parser.add_argument("host", choices=["claude", "codex", "all"], help="Target host.")
    parser.add_argument("--target", type=Path, help="Override target directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest_path = _manifest_path()
        manifest = _load_manifest(manifest_path)
        manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
        manifest.setdefault("hosts", {})
        for host, label, installer in (
            ("claude", "Claude Code", install_claude),
            ("codex", "Codex", install_codex),
        ):
            if args.host not in (host, "all"):
                continue
            print(f"Installing {label} integrations...")
            _purge_previous(host, manifest)
            result = installer(args.target)
            manifest["hosts"][host] = {
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "package_version": _package_version(),
                "files": result["files"],
                "created_dirs": result["created_dirs"],
            }
            _write_manifest(manifest_path, manifest)
        print("Done.")
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1


def _uninstall_host(entry: dict) -> None:
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
    parser = argparse.ArgumentParser(description="Uninstall pilot-workers integrations.")
    parser.add_argument("host", choices=["claude", "codex", "all"], help="Target host.")
    args = parser.parse_args(argv)

    hosts = ["claude", "codex"] if args.host == "all" else [args.host]
    manifest_path = _manifest_path()
    if not manifest_path.exists():
        print(f"error: no install manifest found at {manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    entries = manifest.get("hosts", {})
    present = [h for h in hosts if h in entries]
    missing = [h for h in hosts if h not in entries]
    if missing and not present:
        print(f"error: no manifest entry for host(s): {', '.join(missing)}",
              file=sys.stderr)
        return 1
    if missing:
        for h in missing:
            print(f"note: no manifest entry for {h}, skipping", file=sys.stderr)
    try:
        for host in present:
            print(f"Uninstalling {host} integrations...")
            _uninstall_host(entries.pop(host))
        if entries:
            manifest["hosts"] = entries
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
