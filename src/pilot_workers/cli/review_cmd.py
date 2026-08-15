"""pw9 review — auto-fanout code review by configured axes.

``pw9 review --provider ds --workdir .`` generates one task file per axis
from the effective strategy config, then fanouts them in parallel.
``pw9 review add/edit/remove/show`` manage the axis list.
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
  pw9 review --provider <key>[,<key>,...] --workdir <dir>
             [--replicate] [--raw] [--timeout <sec>] [--dry-run]
  pw9 review add <name>            # opens $EDITOR to write the focus
  pw9 review edit [<name>]         # edit one axis or the whole config
  pw9 review remove <name>
  pw9 review show

Several providers are round-robin assigned across axes (shares the load).
--replicate gives every axis to every provider instead: the same scope seen
by independent models, which is what makes cross-model review worth its
cost. Findings are merged by location and marked with how many models
flagged each; --raw prints the unmerged verdict array.
"""

_MODE = "review"

# One source for the per-job budget: the generated skill quotes this number,
# so a literal here and prose there would drift apart on the first change.
DEFAULT_TIMEOUT_S = 900


# ------------------------------------------------------------------
# axis management (add / edit / remove / show)
# ------------------------------------------------------------------

def _open_editor(initial: str, suffix: str = ".md") -> str | None:
    """Open $EDITOR on a temp file, return content or None if unchanged/empty."""
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, prefix="pw9-review-",
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
        # Strip comment lines.
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
        print(f"error: invalid axis name: {name!r}", file=sys.stderr)
        return 2
    existing = strategies.effective(_MODE)
    if any(item["name"] == name for item in existing):
        print(f"error: axis {name!r} already exists; use 'review edit {name}'",
              file=sys.stderr)
        return 1
    template = (
        f"# Axis: {name}\n"
        f"# Write the review focus for this axis below.\n"
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
        # Edit the whole effective config as YAML-ish text.
        items = strategies.effective(_MODE)
        lines = [f"# Review axes — edit and save. Lines starting with # are ignored.\n"]
        for item in items:
            lines.append(f"[{item['name']}]")
            lines.append(item.get("focus", ""))
            lines.append("")
        text = _open_editor("\n".join(lines))
        if text is None:
            print("cancelled (unchanged)")
            return 0
        # Parse the block format.
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
        print(f"  updated {len(parsed)} axes")
        return 0
    # Edit a single axis.
    items = strategies.effective(_MODE)
    match = next((i for i in items if i["name"] == name), None)
    if match is None:
        print(f"error: no axis {name!r}; see 'pw9 review show'", file=sys.stderr)
        return 1
    template = (
        f"# Axis: {name}\n"
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
        print(f"note: no axis {name!r}")
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
        print("(no axes configured)")
        return 0
    print(f"review axes ({len(items)}):\n")
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

def _generate_task(axis: dict[str, str], workdir: str) -> str:
    """Generate a review task file for one axis."""
    name = axis["name"]
    focus = axis.get("focus", "")
    fd, path_str = tempfile.mkstemp(
        suffix=".md", prefix=f"pw9-review-{name.replace('/', '_')}-")
    os.close(fd)
    os.chmod(path_str, 0o600)
    path = Path(path_str)
    content = f"""<!-- pw9 auto-generated review task — axis: {name} -->

# Review Target

The current working diff in {workdir}.
Run `git diff HEAD` to see staged+unstaged changes; check `git status --porcelain`
for untracked files. Review ALL changed and new files.

# Focus: {name}

{focus}

# Directions

- Sweep every file in the diff against this axis.
- Every finding must have: severity (high/medium/low), exact file:line,
  observed behavior, concrete impact, specific fix direction.
- A suspicion you could not settle: report as [unverified] with the
  command that would confirm it.
- Coverage ledger required: one line per file in scope (examined / skipped + why).

# Already Fixed — do not re-report

none
"""
    path.write_text(content, encoding="utf-8")
    return path_str


_SEVERITY_ORDER = {"high": 3, "medium": 2, "low": 1}


def _last_json_array(text: str) -> list[dict]:
    """fanout's stdout is started lines then ONE verdict array, last."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                return []
            return [d for d in data if isinstance(d, dict)]
    return []


def build_plan(axes: list[dict], provider_list: list[str],
               replicate: bool) -> list[tuple[dict, str]]:
    """Which provider reviews which axis.

    Default is round-robin: the axes are SPREAD across providers, which
    shares the load but leaves every axis seen by exactly one model.
    ``replicate`` instead gives every axis to every provider — the shape
    cross-model review actually needs, since its whole premise is that two
    models' blind spots are uncorrelated, and that only pays off when both
    look at the SAME scope.
    """
    if replicate:
        return [(axis, p) for axis in axes for p in provider_list]
    return [(axis, provider_list[i % len(provider_list)])
            for i, axis in enumerate(axes)]


def merge_findings(verdicts: list[dict],
                   plan: list[tuple[dict, str]]) -> list[dict]:
    """Group findings across axes by location, keeping provenance.

    Deterministic — no model involved. Findings at the same `file_line` are
    grouped, never dropped: two axes describing different defects on one
    line are both real, so each keeps its own summary. What the grouping
    adds is `found_by`, which is the whole point under ``--replicate``: a
    location several independent models flagged is worth more of the
    planner's attention than one a single model raised.
    """
    grouped: dict[str, dict] = {}
    for index, verdict in enumerate(verdicts):
        if index < len(plan):
            axis_name = plan[index][0].get("name", "?")
            provider = plan[index][1]
        else:
            axis_name, provider = "?", str(verdict.get("provider") or "?")
        result = verdict.get("result")
        if not isinstance(result, dict):
            continue
        for finding in result.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            location = str(finding.get("file_line") or "(no location)")
            entry = grouped.setdefault(location, {
                "file_line": location,
                "severity": "low",
                "found_by": [],
                "findings": [],
            })
            severity = str(finding.get("severity") or "low")
            if _SEVERITY_ORDER.get(severity, 0) > _SEVERITY_ORDER.get(
                    entry["severity"], 0):
                entry["severity"] = severity
            source = f"{axis_name}/{provider}"
            if source not in entry["found_by"]:
                entry["found_by"].append(source)
            entry["findings"].append(finding)
    merged = list(grouped.values())
    merged.sort(key=lambda e: (-_SEVERITY_ORDER.get(e["severity"], 0),
                               -len(e["found_by"]), e["file_line"]))
    return merged


def _print_merged(merged: list[dict], verdicts: list[dict]) -> None:
    counts = {"high": 0, "medium": 0, "low": 0}
    for entry in merged:
        counts[entry["severity"]] = counts.get(entry["severity"], 0) + 1
    print(f"\n{len(merged)} locations — "
          f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low",
          file=sys.stderr)
    for entry in merged:
        confirms = len(entry["found_by"])
        mark = f" [{confirms}x]" if confirms > 1 else ""
        print(f"\n{entry['severity'].upper():6} {entry['file_line']}{mark}"
              f"  ({', '.join(entry['found_by'])})", file=sys.stderr)
        for finding in entry["findings"]:
            print(f"       {finding.get('summary', '')}", file=sys.stderr)
    reports = [v.get("final_text_path") for v in verdicts
               if v.get("final_text_path")]
    if reports:
        print("\nfull reports:", file=sys.stderr)
        for path in reports:
            print(f"  {path}", file=sys.stderr)


def _cmd_run(provider_list: list[str], workdir: str, timeout: int,
             replicate: bool = False, raw: bool = False) -> int:
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2
    workdir_path = Path(workdir).resolve()
    if not workdir_path.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    axes = strategies.effective(_MODE)
    if not axes:
        print("error: no review axes configured; run 'pw9 review show'",
              file=sys.stderr)
        return 1

    plan = build_plan(axes, provider_list, replicate)
    label = ",".join(provider_list)
    print(f"review: {len(plan)} jobs "
          f"({len(axes)} axes × {len(provider_list)} provider(s): {label})"
          + (" [replicated]" if replicate else ""), file=sys.stderr)
    for axis, p in plan:
        print(f"  → {axis['name']} ({p})", file=sys.stderr)

    task_files: list[str] = []
    fanout_argv = ["--workdir", str(workdir_path)]
    for axis, p in plan:
        path = _generate_task(axis, str(workdir_path))
        task_files.append(path)
        fanout_argv.extend(["--job", f"{p}:review:{path}"])
    fanout_argv.extend(["--timeout", str(timeout)])

    from pilot_workers.cli.fanout import main as fanout_main

    if raw:
        rc = fanout_main(fanout_argv)
    else:
        # fanout owns the stdout contract (started lines + one verdict
        # array). Capture it so the findings can be merged before anything
        # reaches the planner — protecting the context this tool exists to
        # protect — then re-emit the array so `--json`-style consumers and
        # pipelines still see exactly what fanout produced.
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = fanout_main(fanout_argv)
        captured = buffer.getvalue()
        print(captured, end="")
        verdicts = _last_json_array(captured)
        if verdicts:
            _print_merged(merge_findings(verdicts, plan), verdicts)

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
            print("usage: pw9 review add <name>", file=sys.stderr)
            return 2
        return _cmd_add(args[1])

    if verb == "edit":
        return _cmd_edit(args[1] if len(args) > 1 else None)

    if verb == "remove":
        if len(args) != 2:
            print("usage: pw9 review remove <name>", file=sys.stderr)
            return 2
        return _cmd_remove(args[1])

    if verb == "show":
        return _cmd_show()

    # Execution mode: pw9 review --provider <key> --workdir <dir>
    provider = None
    workdir = None
    timeout = DEFAULT_TIMEOUT_S
    dry_run = False
    replicate = False
    raw = False
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
        elif args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--replicate":
            replicate = True
            i += 1
        elif args[i] == "--raw":
            raw = True
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
        axes = strategies.effective(_MODE)
        plan = build_plan(axes, provider_list, replicate)
        print(json.dumps({
            "mode": "review",
            "providers": provider_list,
            "replicate": replicate,
            "jobs": len(plan),
            "plan": [{"axis": a["name"], "provider": p} for a, p in plan],
            "workdir": workdir,
        }, indent=2))
        return 0

    return _cmd_run(provider_list, workdir, timeout,
                    replicate=replicate, raw=raw)
