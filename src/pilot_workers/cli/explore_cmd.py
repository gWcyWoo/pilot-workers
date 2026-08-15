"""pw9 explore — auto-fanout exploration by configured lenses.

``pw9 explore --provider ds,glm --workdir . --requirement "refactor payment callback to async"``
generates one task file per lens (flow / constraints / impact / abstraction),
round-robin assigns providers, and fanouts them in parallel.
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
  pw9 explore --provider <key>[,<key>,...] --workdir <dir> --requirement "<text>"
  pw9 explore --provider <key>[,<key>,...] --workdir <dir> --requirement-file <path>
              [--out <path>] [--timeout <sec>] [--dry-run]
  pw9 explore add <name>            # opens $EDITOR to write the focus
  pw9 explore edit [<name>]         # edit one lens or the whole config
  pw9 explore remove <name>
  pw9 explore show

Multiple providers are round-robin assigned across lenses.
All 4 lenses are dispatched by default; manage with add/edit/remove.

--out      write the verdict array to a file. These commands run in the
           foreground for minutes; a host shell that cuts off at its own
           timeout kills the fanout. Background the call and read the file.
"""

_MODE = "explore"


# ------------------------------------------------------------------
# lens management (add / edit / remove / show) — same pattern as review
# ------------------------------------------------------------------

def _open_editor(initial: str, suffix: str = ".md") -> str | None:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, prefix="pw9-explore-",
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
        print(f"error: invalid lens name: {name!r}", file=sys.stderr)
        return 2
    existing = strategies.effective(_MODE)
    if any(item["name"] == name for item in existing):
        print(f"error: lens {name!r} already exists; use 'pw9 explore edit {name}'",
              file=sys.stderr)
        return 1
    template = (
        f"# Lens: {name}\n"
        f"# Write the exploration focus for this lens below.\n"
        f"# Lines starting with # are ignored. Save and quit to confirm.\n\n"
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
        lines = ["# Explore lenses — edit and save. Lines starting with # are ignored.\n"]
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
        print(f"  updated {len(parsed)} lenses")
        return 0
    items = strategies.effective(_MODE)
    match = next((i for i in items if i["name"] == name), None)
    if match is None:
        print(f"error: no lens {name!r}; see 'pw9 explore show'", file=sys.stderr)
        return 1
    template = (
        f"# Lens: {name}\n"
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
        print(f"note: no lens {name!r}")
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
        print("(no lenses configured)")
        return 0
    print(f"explore lenses ({len(items)}):\n")
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

def _generate_task(lens: dict[str, str], workdir: str,
                   requirement: str) -> str:
    name = lens["name"]
    focus = lens.get("focus", "")
    fd, path_str = tempfile.mkstemp(
        suffix=".md", prefix=f"pw9-explore-{name.replace('/', '_')}-")
    os.close(fd)
    os.chmod(path_str, 0o600)
    content = f"""<!-- pw9 auto-generated explore task — lens: {name} -->

# Requirement

{requirement}

# Exploration Lens: {name}

{focus}

# Directions

- Answer exactly what this lens asks, scoped to the requirement above.
- Every conclusion must carry a file:line reference.
- Facts only, no judgment — no design recommendations or trade-off conclusions.
- Output structured items, one fact per item.
- Cap conclusions at 20 items unless the scope warrants more.
"""
    Path(path_str).write_text(content, encoding="utf-8")
    return path_str


def _last_json_array(text: str) -> list[dict]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return []
            return [d for d in data if isinstance(d, dict)]
    return []


def run_lenses(provider_list: list[str], workdir_path: Path, requirement: str,
               timeout: int, *, capture: bool = False,
               ) -> tuple[int, list[dict], list[dict]]:
    """Fan the configured lenses out. Returns (rc, verdicts, lens_plan).

    ``capture`` swallows fanout's stdout and hands the verdicts back instead
    of printing them — that is how `pw9 spec` consumes the same exploration
    without the planner having to read it twice.
    """
    lenses = strategies.effective(_MODE)
    if not lenses:
        print("error: no explore lenses configured; run 'pw9 explore show'",
              file=sys.stderr)
        return 1, [], []

    plan = [(lens, provider_list[i % len(provider_list)])
            for i, lens in enumerate(lenses)]
    label = ",".join(provider_list)
    print(f"explore: {len(lenses)} lenses × {len(provider_list)} provider(s) "
          f"({label})", file=sys.stderr)
    for lens, provider in plan:
        print(f"  → {lens['name']} ({provider})", file=sys.stderr)

    task_files: list[str] = []
    fanout_argv = ["--workdir", str(workdir_path)]
    for lens, provider in plan:
        path = _generate_task(lens, str(workdir_path), requirement)
        task_files.append(path)
        fanout_argv.extend(["--job", f"{provider}:explore:{path}"])
    fanout_argv.extend(["--timeout", str(timeout)])

    from pilot_workers.cli.fanout import main as fanout_main

    verdicts: list[dict] = []
    if capture:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = fanout_main(fanout_argv)
        verdicts = _last_json_array(buffer.getvalue())
    else:
        rc = fanout_main(fanout_argv)

    for path in task_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    return rc, verdicts, [lens for lens, _ in plan]


def _cmd_run(provider_list: list[str], workdir: str, requirement: str,
             timeout: int, out_path: str | None = None) -> int:
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2
    workdir_path = Path(workdir).resolve()
    if not workdir_path.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    if out_path:
        rc, verdicts, _ = run_lenses(provider_list, workdir_path, requirement,
                                     timeout, capture=True)
        Path(out_path).write_text(json.dumps(verdicts, indent=2),
                                  encoding="utf-8")
        print(json.dumps(verdicts))
        print(f"  verdicts written: {out_path}", file=sys.stderr)
    else:
        rc, _, _ = run_lenses(provider_list, workdir_path, requirement, timeout)
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
            print("usage: pw9 explore add <name>", file=sys.stderr)
            return 2
        return _cmd_add(args[1])

    if verb == "edit":
        return _cmd_edit(args[1] if len(args) > 1 else None)

    if verb == "remove":
        if len(args) != 2:
            print("usage: pw9 explore remove <name>", file=sys.stderr)
            return 2
        return _cmd_remove(args[1])

    if verb == "show":
        return _cmd_show()

    # Execution mode
    provider = None
    workdir = None
    requirement = None
    requirement_file = None
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
        elif args[i] == "--requirement" and i + 1 < len(args):
            requirement = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out_path = args[i + 1]
            i += 2
        elif args[i] == "--requirement-file" and i + 1 < len(args):
            requirement_file = args[i + 1]
            i += 2
        elif args[i] == "--timeout" and i + 1 < len(args):
            try:
                timeout = int(args[i + 1])
            except ValueError:
                print(f"error: --timeout requires an integer, got {args[i + 1]!r}",
                      file=sys.stderr)
                return 2
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

    if requirement_file:
        rf = Path(requirement_file)
        if not rf.is_file():
            print(f"error: requirement file not found: {requirement_file}",
                  file=sys.stderr)
            return 2
        requirement = rf.read_text(encoding="utf-8").strip()

    if not requirement:
        print("error: --requirement or --requirement-file is required",
              file=sys.stderr)
        return 2

    provider_list = [p.strip() for p in provider.split(",") if p.strip()]
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2

    if dry_run:
        lenses = strategies.effective(_MODE)
        plan = []
        for i, l in enumerate(lenses):
            plan.append({"lens": l["name"],
                         "provider": provider_list[i % len(provider_list)]})
        print(json.dumps({
            "mode": "explore",
            "providers": provider_list,
            "requirement": requirement[:200],
            "plan": plan,
            "workdir": workdir,
        }, indent=2))
        return 0

    return _cmd_run(provider_list, workdir, requirement, timeout, out_path)
