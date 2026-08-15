"""pw9 spec — turn an exploration into a DRAFT code task contract.

`code` is the one mode that still needs a hand-written contract, which is
also the mode whose contract costs the most to write: objective, entry
points, callers, constraining tests. Writing it well requires exactly the
bulk repo-reading this tool exists to move off the planner.

So: run the explore lenses, then assemble their facts into a draft of the
code template. The worker proposes; the planner disposes. Everything that
is an architecture decision — the objective, the locked approach, the
scope boundary, how it gets verified — is left as a PILOT_FILL
placeholder, which `taskguard` REFUSES to dispatch. That refusal is the
feature: an unedited draft physically cannot be sent to a worker.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from pilot_workers.providers import PROVIDERS

USAGE = """usage:
  pw9 spec --provider <key>[,<key>,...] --workdir <dir>
           --requirement "<text>" | --requirement-file <path>
           [--out <path>] [--timeout <sec>] [--dry-run]

Explores first, then drafts a code task file from what it found. The draft
is NOT dispatchable as-is: the decisions only you can make are left as
PILOT_FILL placeholders, and taskguard refuses a task that still has them.
Edit it, then: pw9 dispatch --provider <key> --mode code --task-file <path>
"""

DEFAULT_TIMEOUT_S = 900

# Which lens feeds which section. Facts about how things work today and what
# the new code must fit into are Known Context; facts about what else the
# change touches suggest the scope boundary; duplication evidence informs a
# decision the planner has to make, so it is quoted under Locked Decisions
# rather than turned into one.
_CONTEXT_LENSES = ("flow", "constraints")
_SCOPE_LENSES = ("impact",)
_DECISION_LENSES = ("abstraction",)


def _facts_by_lens(verdicts: list[dict],
                   lenses: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for index, verdict in enumerate(verdicts):
        name = (lenses[index].get("name", "?") if index < len(lenses)
                else str(verdict.get("provider") or "?"))
        result = verdict.get("result")
        if not isinstance(result, dict):
            continue
        facts = [f for f in (result.get("facts") or []) if isinstance(f, dict)]
        out.setdefault(name, []).extend(facts)
    return out


def _file_of(file_line: str) -> str:
    """`src/x.py:42` -> `src/x.py`. Windows drive letters are not a concern
    here: these come from the worker's own repo-relative citations."""
    return file_line.rsplit(":", 1)[0] if ":" in file_line else file_line


def _bullets(facts: list[dict]) -> str:
    if not facts:
        return "_(the exploration returned nothing for this lens)_"
    lines = []
    for fact in facts:
        text = str(fact.get("fact", "")).strip()
        where = str(fact.get("file_line", "")).strip()
        if not text:
            continue
        lines.append(f"- {text}" + (f"  (`{where}`)" if where else ""))
    return "\n".join(lines) or "_(no usable facts)_"


def build_draft(requirement: str, by_lens: dict[str, list[dict]]) -> str:
    """Assemble the code template with the explored facts filled in.

    Deterministic string assembly — no model decides what goes where. The
    sections a worker cannot legitimately fill keep their PILOT_FILL
    markers, so the draft stays undispatchable until a human edits it.
    """
    context_facts: list[dict] = []
    for lens in _CONTEXT_LENSES:
        context_facts.extend(by_lens.get(lens, []))
    scope_facts: list[dict] = []
    for lens in _SCOPE_LENSES:
        scope_facts.extend(by_lens.get(lens, []))
    decision_facts: list[dict] = []
    for lens in _DECISION_LENSES:
        decision_facts.extend(by_lens.get(lens, []))

    touched = sorted({_file_of(str(f.get("file_line", "")))
                      for f in scope_facts + context_facts
                      if f.get("file_line")})
    scope_suggestion = "\n".join(f"- `{path}`" for path in touched[:25])
    if len(touched) > 25:
        scope_suggestion += f"\n- _(+{len(touched) - 25} more — see Known Context)_"

    return f"""<!-- pw9 DRAFT · code mode. Sections below marked PILOT_FILL are
     yours to decide: taskguard refuses to dispatch a task that still
     contains them, so this file cannot be sent to a worker unedited. -->

# Objective

<!--PILOT_FILL Observable completion-result checklist; each item corresponds to one verification command in Verification. The exploration below says what IS; this says what must become true. -->

Requirement as given to the exploration:

> {requirement}

# Locked Decisions

<!--PILOT_FILL Settled approach/interface/naming the worker must not redesign. If the approach is not yet decided, do not dispatch. -->

Evidence the exploration found that bears on this decision:

{_bullets(decision_facts)}

# Allowed Scope

<!--PILOT_FILL Confirm or cut the list below, and add the files that must NOT be changed. A scope this draft guessed is not a boundary until you say it is. -->

Files the exploration touched (suggestion, not a boundary):

{scope_suggestion or "_(none identified)_"}

# Known Context

_Assembled from the exploration — facts, not decisions._

{_bullets(context_facts)}

What else the change reaches:

{_bullets(scope_facts)}

# Work

<!--PILOT_FILL What to change and how; use precise paths — never say "that file". -->

# Verification

<!--PILOT_FILL Sub-second verification commands (grep/diff/typecheck/single-file tests), each matching an Objective item one-to-one; leave the heavyweight full test suite to the main session. -->
"""


def _read(path_str: str, label: str) -> str | None:
    path = Path(path_str)
    if not path.is_file():
        print(f"error: {label} file not found: {path_str}", file=sys.stderr)
        return None
    return path.read_text(encoding="utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    provider = workdir = requirement = None
    requirement_file = out_path = None
    timeout = DEFAULT_TIMEOUT_S
    dry_run = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--provider", "--workdir", "--requirement",
                   "--requirement-file", "--out", "--timeout") and i + 1 < len(args):
            value = args[i + 1]
            if arg == "--provider":
                provider = value
            elif arg == "--workdir":
                workdir = value
            elif arg == "--requirement":
                requirement = value
            elif arg == "--requirement-file":
                requirement_file = value
            elif arg == "--out":
                out_path = value
            else:
                try:
                    timeout = int(value)
                except ValueError:
                    print(f"error: --timeout requires an integer, got {value!r}",
                          file=sys.stderr)
                    return 2
            i += 2
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(f"error: unexpected argument: {arg}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return 2

    if not provider or not workdir:
        print("error: --provider and --workdir are required", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2
    if requirement_file:
        requirement = _read(requirement_file, "requirement")
        if requirement is None:
            return 2
    if not requirement:
        print("error: --requirement or --requirement-file is required",
              file=sys.stderr)
        return 2

    provider_list = [p.strip() for p in provider.split(",") if p.strip()]
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2

    workdir_path = Path(workdir).resolve()
    if not workdir_path.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    if dry_run:
        import json

        from pilot_workers import strategies

        lenses = strategies.effective("explore")
        print(json.dumps({
            "mode": "spec",
            "providers": provider_list,
            "explores": [l["name"] for l in lenses],
            "requirement": requirement[:200],
            "out": out_path,
            "workdir": str(workdir_path),
        }, indent=2))
        return 0

    from pilot_workers.cli.explore_cmd import run_lenses

    rc, verdicts, lenses = run_lenses(
        provider_list, workdir_path, requirement, timeout, capture=True)
    if not verdicts:
        print("error: exploration produced no verdicts; nothing to draft from",
              file=sys.stderr)
        return rc or 1

    draft = build_draft(requirement, _facts_by_lens(verdicts, lenses))

    if out_path:
        target = Path(out_path)
    else:
        fd, name = tempfile.mkstemp(suffix=".md", prefix="pw9-spec-")
        os.close(fd)
        target = Path(name)
    target.write_text(draft, encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    print(f"\n  draft written: {target}", file=sys.stderr)
    print(f"  it is NOT dispatchable yet — fill the PILOT_FILL sections "
          f"(Objective, Locked Decisions, Allowed Scope, Work, Verification),",
          file=sys.stderr)
    print(f"  then: pw9 dispatch --provider <key> --mode code "
          f"--workdir {workdir_path} --task-file {target}", file=sys.stderr)
    return rc
