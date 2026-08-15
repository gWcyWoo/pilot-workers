"""pw9 provider — manage user-level provider configurations.

``pw9 provider add <key>`` opens $EDITOR with a YAML template;
``pw9 provider edit <key>`` edits an existing provider (user override
or package default); ``pw9 provider remove <key>`` deletes a user
override; ``pw9 provider show`` lists all effective providers.

User overrides are stored in ``$PILOT_WORKERS_HOME/providers/<key>.yaml``
and merged on top of package defaults at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pilot_workers import providers as providers_mod

USAGE = """usage:
  pw9 provider add <key>       # opens $EDITOR with a YAML template
  pw9 provider edit <key>      # edit an existing provider
  pw9 provider remove <key>    # delete a user override
  pw9 provider show            # list all effective providers
"""

TEMPLATE = """\
# Provider configuration for {key}
# Fill in the required fields and save. Lines starting with # are comments.

key: {key}
provider_id: {key}-worker
model_id: <model-id>
base_url: <https://api.example.com/v1>
display_name: <Human-Readable Name>
context_tokens: 128000
output_tokens: 16000
strengths: <what this model is good at>
suitable_modes: code, explore, test, review
notes: <any notes for the user>
"""


def _open_editor(initial: str) -> str | None:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="pw9-provider-",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(initial)
        f.flush()
        path = f.name
    try:
        import shlex
        mtime_before = os.path.getmtime(path)
        try:
            cmd = [*shlex.split(editor), path]
        except ValueError:
            cmd = [editor, path]
        subprocess.run(cmd, check=True)
        mtime_after = os.path.getmtime(path)
        if mtime_after == mtime_before:
            return None
        return Path(path).read_text(encoding="utf-8").strip() or None
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"error: editor failed: {exc}", file=sys.stderr)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _cmd_add(key: str) -> int:
    if "/" in key or "\\" in key or not key.strip():
        print(f"error: invalid provider key: {key!r}", file=sys.stderr)
        return 2
    if key in providers_mod.RESERVED_PROVIDER_KEYS:
        print(f"error: reserved key: {key}", file=sys.stderr)
        return 2
    user_dir = providers_mod.user_providers_dir()
    user_path = user_dir / f"{key}.yaml"
    if user_path.is_file():
        print(f"error: user provider {key!r} already exists; use 'pw9 provider edit {key}'",
              file=sys.stderr)
        return 1
    if key in providers_mod.PROVIDERS:
        print(f"note: {key!r} exists as a package default; your override will replace it",
              file=sys.stderr)
    text = _open_editor(TEMPLATE.format(key=key))
    if not text:
        print("cancelled (empty or unchanged)")
        return 0
    user_dir.mkdir(parents=True, exist_ok=True)
    user_path.write_text(text + "\n", encoding="utf-8")
    os.chmod(str(user_path), 0o600)
    print(f"  wrote {user_path}")
    print(f"  run 'pw9 key {key}' to configure the API key")
    return 0


def _cmd_edit(key: str) -> int:
    user_dir = providers_mod.user_providers_dir()
    user_path = user_dir / f"{key}.yaml"
    if user_path.is_file():
        initial = user_path.read_text(encoding="utf-8")
    elif key in providers_mod.PROVIDERS:
        pkg_path = providers_mod.PROVIDERS_DIR / f"{key}.yaml"
        if pkg_path.is_file():
            initial = pkg_path.read_text(encoding="utf-8")
        else:
            initial = TEMPLATE.format(key=key)
    else:
        print(f"error: no provider {key!r}; use 'pw9 provider add {key}'",
              file=sys.stderr)
        return 1
    text = _open_editor(initial)
    if not text:
        print("cancelled (unchanged)")
        return 0
    user_dir.mkdir(parents=True, exist_ok=True)
    user_path.write_text(text + "\n", encoding="utf-8")
    os.chmod(str(user_path), 0o600)
    print(f"  updated {user_path}")
    return 0


def _cmd_remove(key: str) -> int:
    user_path = providers_mod.user_providers_dir() / f"{key}.yaml"
    if not user_path.is_file():
        print(f"note: no user override for {key!r}")
        return 0
    user_path.unlink()
    if key in providers_mod.PROVIDERS:
        pkg_path = providers_mod.PROVIDERS_DIR / f"{key}.yaml"
        if pkg_path.is_file():
            print(f"  removed user override; package default for {key!r} is now active")
        else:
            print(f"  removed {key!r}")
    else:
        print(f"  removed {key!r}")
    return 0


def _cmd_show() -> int:
    if not providers_mod.PROVIDERS:
        print("(no providers configured)")
        return 0
    user_dir = providers_mod.user_providers_dir()
    print(f"providers ({len(providers_mod.PROVIDERS)}):\n")
    for key in sorted(providers_mod.PROVIDERS):
        p = providers_mod.PROVIDERS[key]
        user_path = user_dir / f"{key}.yaml"
        tag = "  [user]" if user_path.is_file() else ""
        print(f"  {key}{tag}")
        print(f"    model: {p.model_id}")
        print(f"    auth: {p.auth}" + (f" ({p.auth_method})" if p.auth_method else ""))
        # An oauth provider has no base_url — the runner's built-in integration
        # carries it. Printing an empty value reads as a missing one.
        print(f"    endpoint: {p.base_url or f'(built into runner {p.runner})'}")
        if p.strengths:
            print(f"    strengths: {p.strengths}")
        if p.suitable_modes:
            print(f"    modes: {p.suitable_modes}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    verb = args[0]

    try:
        if verb == "add":
            if len(args) != 2:
                print("usage: pw9 provider add <key>", file=sys.stderr)
                return 2
            return _cmd_add(args[1])

        if verb == "edit":
            if len(args) != 2:
                print("usage: pw9 provider edit <key>", file=sys.stderr)
                return 2
            return _cmd_edit(args[1])

        if verb == "remove":
            if len(args) != 2:
                print("usage: pw9 provider remove <key>", file=sys.stderr)
                return 2
            return _cmd_remove(args[1])

        if verb == "show":
            return _cmd_show()
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"error: unknown provider verb: {verb}", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2
