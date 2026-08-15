"""pw9 test — auto-fanout test execution by configured layers.

``pw9 test --provider ds --workdir .`` generates one task file per test
layer from the effective strategy config, then fanouts them in parallel.
``pw9 test add/edit/remove/show`` manage the layer list.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from pathlib import Path

from pilot_workers import strategies
from pilot_workers.providers import PROVIDERS

USAGE = """usage:
  pw9 test --provider <key> --workdir <dir>
           [--known-failures <path>] [--out <path>]
           [--timeout <sec>] [--dry-run]
  pw9 test add <name>             # opens $EDITOR to write the focus
  pw9 test edit [<name>]          # edit one layer or the whole config
  pw9 test remove <name>
  pw9 test show

Runs the suite in a worker so its output dies there instead of in the
planner's context; only the failures come back.

--known-failures  a file listing failures already known to be broken, so
                  they are not reported as new. Several providers are
                  round-robin assigned across layers, but note that a suite
                  gives the same pass/fail on any model — extra providers
                  buy nothing here.

--out      write the verdict array to a file. These commands run in the
           foreground for minutes; a host shell that cuts off at its own
           timeout kills the fanout. Background the call and read the file.
"""

_MODE = "test"


# ------------------------------------------------------------------
# layer management (add / edit / remove / show)
# ------------------------------------------------------------------

def _open_editor(initial: str, suffix: str = ".md") -> str | None:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, prefix="pw9-test-",
        delete=False, encoding="utf-8",
    ) as f:
        f.write(initial)
        f.flush()
        path = f.name
    try:
        mtime_before = os.path.getmtime(path)
        import shlex
        try:
            cmd = [*shlex.split(editor), path]
        except ValueError:
            cmd = [editor, path]
        subprocess.run(cmd, check=True)
        mtime_after = os.path.getmtime(path)
        if mtime_after == mtime_before:
            return None
        text = Path(path).read_text(encoding="utf-8").strip()
        lines = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        return "\n".join(lines).strip() or None
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"error: editor failed: {exc}", file=sys.stderr)
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _cmd_add(name: str) -> int:
    if "/" in name or "\\" in name or not name.strip():
        print(f"error: invalid layer name: {name!r}", file=sys.stderr)
        return 2
    existing = strategies.effective(_MODE)
    if any(item["name"] == name for item in existing):
        print(f"error: layer {name!r} already exists; use 'pw9 test edit {name}'",
              file=sys.stderr)
        return 1
    template = (
        f"# Layer: {name}\n"
        f"# Write the test focus for this layer below.\n"
        f"# Lines starting with # are ignored. Save and quit to confirm.\n"
        f"# Leave empty or quit without saving to cancel.\n\n"
    )
    focus = _open_editor(template)
    if not focus:
        print("cancelled (empty or unchanged)")
        return 0
    strategies.add_item(_MODE, name, focus)
    print(f"  added: {name}")
    return 0


def _cmd_edit(name: str | None) -> int:
    if name is None:
        items = strategies.effective(_MODE)
        lines = ["# Test layers — edit and save. Lines starting with # are ignored.\n"]
        for item in items:
            lines.append(f"[{item['name']}]")
            lines.append(item.get("focus", ""))
            lines.append("")
        text = _open_editor("\n".join(lines))
        if text is None:
            print("cancelled (unchanged)")
            return 0
        current_name = None
        current_focus: list[str] = []
        parsed: list[tuple[str, str]] = []
        for line in text.splitlines():
            if line.startswith("[") and line.endswith("]"):
                if current_name:
                    parsed.append((current_name, "\n".join(current_focus).strip()))
                current_name = line[1:-1].strip()
                current_focus = []
            elif current_name is not None:
                current_focus.append(line)
        if current_name:
            parsed.append((current_name, "\n".join(current_focus).strip()))
        if not parsed:
            print("cancelled (no [name] blocks found)")
            return 0
        parsed_names = {n for n, _ in parsed}
        existing = {item.get("name"): item.get("focus", "") for item in items}
        for n, f in parsed:
            if existing.get(n) != f:
                strategies.edit_item(_MODE, n, f)
        for item in items:
            if item["name"] not in parsed_names:
                strategies.remove_item(_MODE, item["name"])
        print(f"  updated {len(parsed)} layers")
        return 0
    items = strategies.effective(_MODE)
    match = next((i for i in items if i["name"] == name), None)
    if match is None:
        print(f"error: no layer {name!r}; see 'pw9 test show'", file=sys.stderr)
        return 1
    template = (
        f"# Layer: {name}\n"
        f"# Edit the focus below. Lines starting with # are ignored.\n\n"
        f"{match.get('focus', '')}\n"
    )
    focus = _open_editor(template)
    if focus is None:
        print("cancelled (unchanged)")
        return 0
    strategies.edit_item(_MODE, name, focus)
    print(f"  updated: {name}")
    return 0


def _cmd_remove(name: str) -> int:
    if strategies.remove_item(_MODE, name):
        print(f"  removed: {name}")
    else:
        print(f"note: no layer {name!r}")
    return 0


def _cmd_show() -> int:
    items = strategies.effective(_MODE)
    overrides = strategies.load_overrides(_MODE)
    removed = {n for n in overrides.get("removed", []) if isinstance(n, str)}
    added_names = {i.get("name") for i in overrides.get("added", [])
                   if isinstance(i, dict) and i.get("name")}
    defaults = strategies._load_default(_MODE)
    default_names = {i.get("name") for i in defaults if isinstance(i, dict)}
    if not items:
        print("(no layers configured)")
        return 0
    print(f"test layers ({len(items)}):\n")
    for item in items:
        name = item.get("name", "??")
        tag = ""
        if name in added_names:
            tag = "  [user]" if name not in default_names else "  [edited]"
        print(f"  {name}{tag}")
        focus = item.get("focus", "")
        for line in focus.splitlines():
            print(f"    {line}")
        print()
    if removed:
        print(f"  removed defaults: {', '.join(sorted(removed))}")
    path = strategies.overrides_path(_MODE)
    if path.is_file():
        print(f"  overrides: {path}")
    return 0


# ------------------------------------------------------------------
# auto-fanout execution
# ------------------------------------------------------------------

def _generate_task(layer: dict[str, str], workdir: str,
                   known_failures: str = "") -> str:
    name = layer["name"]
    focus = layer.get("focus", "")
    fd, path_str = tempfile.mkstemp(
        suffix=".md", prefix=f"pw9-test-{name.replace('/', '_')}-")
    os.close(fd)
    os.chmod(path_str, 0o600)
    baseline = known_failures.strip() or "none"
    content = f"""<!-- pw9 auto-generated test task — layer: {name} -->

# Test Target

Project at {workdir}.

# Focus: {name}

{focus}

# Known Pre-existing Failures — do not report these as new

{baseline}

# Directions

- Find the project's test command and run it in full.
- Report the command, exit code, and pass/fail counts.
- For each failure: the full test name and the raw error text.
- A failure listed above as pre-existing is NOT a new finding — say it is
  still failing, but do not present it as something this run discovered.
- Do not diagnose and do not fix. Your job is to run the suite and hand
  back the failures; the planner takes it from there.
"""
    Path(path_str).write_text(content, encoding="utf-8")
    return path_str


def _cmd_run(provider_list: list[str], workdir: str, timeout: int,
             known_failures: str = "", out_path: str | None = None) -> int:
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2
    workdir_path = Path(workdir).resolve()
    if not workdir_path.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    layers = strategies.effective(_MODE)
    if not layers:
        print("error: no test layers configured; run 'pw9 test show'",
              file=sys.stderr)
        return 1

    label = ",".join(provider_list)
    print(f"test: {len(layers)} layers × {len(provider_list)} provider(s) ({label})",
          file=sys.stderr)
    for i, ly in enumerate(layers):
        p = provider_list[i % len(provider_list)]
        print(f"  → {ly['name']} ({p})", file=sys.stderr)

    task_files: list[str] = []
    for layer in layers:
        task_files.append(_generate_task(layer, str(workdir_path),
                                         known_failures))

    fanout_argv = ["--workdir", str(workdir_path)]
    for i, tf in enumerate(task_files):
        p = provider_list[i % len(provider_list)]
        fanout_argv.extend(["--job", f"{p}:test:{tf}"])
    fanout_argv.extend(["--timeout", str(timeout)])

    from pilot_workers.cli.fanout import main as fanout_main

    if out_path:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = fanout_main(fanout_argv)
        captured = buffer.getvalue()
        print(captured, end="")
        for line in reversed(captured.splitlines()):
            if line.strip().startswith("["):
                Path(out_path).write_text(line.strip(), encoding="utf-8")
                print(f"  verdicts written: {out_path}", file=sys.stderr)
                break
    else:
        rc = fanout_main(fanout_argv)

    for tf in task_files:
        try:
            os.unlink(tf)
        except OSError:
            pass

    return rc


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    verb = args[0]

    try:
        return _dispatch(verb, args)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(verb: str, args: list[str]) -> int:
    if verb == "add":
        if len(args) != 2:
            print("usage: pw9 test add <name>", file=sys.stderr)
            return 2
        return _cmd_add(args[1])

    if verb == "edit":
        return _cmd_edit(args[1] if len(args) > 1 else None)

    if verb == "remove":
        if len(args) != 2:
            print("usage: pw9 test remove <name>", file=sys.stderr)
            return 2
        return _cmd_remove(args[1])

    if verb == "show":
        return _cmd_show()

    provider = None
    workdir = None
    known_failures_file = None
    out_path = None
    timeout = 900
    dry_run = False
    i = 0
    while i < len(args):
        if args[i] == "--provider" and i + 1 < len(args):
            provider = args[i + 1]
            i += 2
        elif args[i] == "--workdir" and i + 1 < len(args):
            workdir = args[i + 1]
            i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            try:
                timeout = int(args[i + 1])
            except ValueError:
                print(f"error: --timeout requires an integer, got {args[i + 1]!r}",
                      file=sys.stderr)
                return 2
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_path = args[i + 1]
            i += 2
        elif args[i] == "--known-failures" and i + 1 < len(args):
            known_failures_file = args[i + 1]
            i += 2
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"error: unexpected argument: {args[i]}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return 2
    if not provider or not workdir:
        print("error: --provider and --workdir are required", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    provider_list = [p.strip() for p in provider.split(",") if p.strip()]
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2

    if dry_run:
        layers = strategies.effective(_MODE)
        plan = []
        for i, l in enumerate(layers):
            plan.append({"layer": l["name"],
                         "provider": provider_list[i % len(provider_list)]})
        print(json.dumps({
            "mode": "test",
            "providers": provider_list,
            "plan": plan,
            "workdir": workdir,
        }, indent=2))
        return 0

    known_failures = ""
    if known_failures_file:
        path = Path(known_failures_file)
        if not path.is_file():
            print(f"error: known-failures file not found: {known_failures_file}",
                  file=sys.stderr)
            return 2
        known_failures = path.read_text(encoding="utf-8").strip()

    return _cmd_run(provider_list, workdir, timeout, known_failures, out_path)
