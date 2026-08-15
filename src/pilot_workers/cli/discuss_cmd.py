"""pw9 discuss — the same question to several models, independently.

Unlike review/explore, there are no configured axes: the diversity IS the
providers. Each one answers the same question without seeing the others,
and the output is deliberately NOT a synthesized conclusion — it is the
positions plus where they disagree. Synthesizing would hand the decision
to the tool; the disagreement is what the planner actually needs.

One round per invocation. A second round is the planner passing the first
round's positions back in with ``--rebut``, which keeps pw9 stateless and
leaves "is another round worth it" — and "should a human weigh in" — where
those calls belong.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from pilot_workers.providers import PROVIDERS

USAGE = """usage:
  pw9 discuss --provider <key>[,<key>,...] --workdir <dir>
              --question "<text>" | --question-file <path>
              [--context <path>] [--rebut <path>] [--raw]
              [--timeout <sec>] [--dry-run]

Each provider answers the same question independently and does not see the
others. Output is the positions and where they split — not a merged verdict.

--context  extra material every model should read (design notes, a spec)
--rebut    a previous round's positions; each model must engage with them
"""

_MODE = "discuss"
DEFAULT_TIMEOUT_S = 900


def _generate_task(question: str, workdir: str,
                   context: str = "", rebut: str = "") -> str:
    fd, path_str = tempfile.mkstemp(suffix=".md", prefix="pw9-discuss-")
    os.close(fd)
    os.chmod(path_str, 0o600)
    body = f"""<!-- pw9 auto-generated discuss task -->

# Question

{question}

# Codebase

The project at {workdir}. Read what the question touches; cite `file:line`
for every claim about how the code behaves today.
"""
    if context:
        body += f"""
# Context supplied by the planner

{context}
"""
    if rebut:
        body += f"""
# Positions from the previous round

Other models answered this question already. Their positions follow.
Engage with them directly: say which arguments move you and which do not,
and why. Changing your mind because an argument is better is a good
outcome; changing it to agree is not.

{rebut}
"""
    body += """
# Directions

- Take a position and commit to it. If it genuinely depends, say what on,
  and which way you go under each condition.
- Argue for YOUR position. The planner gets its value from several models
  disagreeing, not from each model listing every side.
- Say what would change your mind — that is what tells the planner which
  evidence is worth finding.
"""
    Path(path_str).write_text(body, encoding="utf-8")
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


def summarize(verdicts: list[dict], provider_list: list[str]) -> dict:
    """Positions side by side, and whether they actually split.

    No merging and no conclusion: the planner decides. The one thing worth
    computing is agreement on `choice`, because that is the difference
    between "the models agree, move on" and "they split, this needs you".
    """
    positions = []
    for index, verdict in enumerate(verdicts):
        provider = (provider_list[index] if index < len(provider_list)
                    else str(verdict.get("provider") or "?"))
        result = verdict.get("result")
        if not isinstance(result, dict):
            positions.append({"provider": provider, "position": None,
                              "verdict": verdict.get("verdict")})
            continue
        positions.append({
            "provider": provider,
            "choice": result.get("choice"),
            "position": result.get("position"),
            "risks": result.get("risks"),
            "would_change_if": result.get("would_change_if"),
            "reasoning": result.get("reasoning") or [],
        })
    answered = [p for p in positions if p.get("position")]
    choices = {p.get("choice") for p in answered if p.get("choice")}
    return {
        "positions": positions,
        "answered": len(answered),
        "distinct_choices": sorted(c for c in choices if c),
        "split": len(choices) > 1,
    }


def _print_summary(summary: dict) -> None:
    out = sys.stderr
    for entry in summary["positions"]:
        if not entry.get("position"):
            print(f"\n=== {entry['provider']} — no position "
                  f"({entry.get('verdict')}) ===", file=out)
            continue
        head = entry["provider"]
        if entry.get("choice"):
            head += f" → {entry['choice']}"
        print(f"\n=== {head} ===", file=out)
        print(entry["position"], file=out)
        for item in entry["reasoning"]:
            if isinstance(item, dict):
                print(f"  - {item.get('point', '')}"
                      f"  [{item.get('evidence', '')}]", file=out)
        if entry.get("risks"):
            print(f"  risk: {entry['risks']}", file=out)
        if entry.get("would_change_if"):
            print(f"  would change if: {entry['would_change_if']}", file=out)

    print("", file=out)
    if summary["split"]:
        print(f"SPLIT — {len(summary['distinct_choices'])} distinct positions: "
              f"{', '.join(summary['distinct_choices'])}", file=out)
        print("The disagreement is the useful part. Decide, or run another "
              "round with --rebut.", file=out)
    elif summary["answered"] > 1:
        print("The models agree. Agreement between models that could not see "
              "each other is weak evidence, not proof — they can share a "
              "blind spot.", file=out)


def _read(path_str: str, label: str) -> str | None:
    path = Path(path_str)
    if not path.is_file():
        print(f"error: {label} file not found: {path_str}", file=sys.stderr)
        return None
    return path.read_text(encoding="utf-8").strip()


def _cmd_run(provider_list: list[str], workdir: str, question: str,
             context: str, rebut: str, timeout: int, raw: bool) -> int:
    workdir_path = Path(workdir).resolve()
    if not workdir_path.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    print(f"discuss: {len(provider_list)} independent positions "
          f"({', '.join(provider_list)})", file=sys.stderr)

    task_files: list[str] = []
    fanout_argv = ["--workdir", str(workdir_path)]
    for provider in provider_list:
        # One task file per provider rather than one shared file: fanout
        # deletes nothing, and a per-provider path keeps the artifacts
        # traceable when a position needs to be re-read later.
        path = _generate_task(question, str(workdir_path), context, rebut)
        task_files.append(path)
        fanout_argv.extend(["--job", f"{provider}:discuss:{path}"])
    fanout_argv.extend(["--timeout", str(timeout)])

    from pilot_workers.cli.fanout import main as fanout_main

    if raw:
        rc = fanout_main(fanout_argv)
    else:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = fanout_main(fanout_argv)
        captured = buffer.getvalue()
        print(captured, end="")
        verdicts = _last_json_array(captured)
        if verdicts:
            _print_summary(summarize(verdicts, provider_list))

    for path in task_files:
        try:
            os.unlink(path)
        except OSError:
            pass
    return rc


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    provider = workdir = question = None
    question_file = context_file = rebut_file = None
    timeout = DEFAULT_TIMEOUT_S
    dry_run = raw = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--provider", "--workdir", "--question", "--question-file",
                   "--context", "--rebut", "--timeout") and i + 1 < len(args):
            value = args[i + 1]
            if arg == "--provider":
                provider = value
            elif arg == "--workdir":
                workdir = value
            elif arg == "--question":
                question = value
            elif arg == "--question-file":
                question_file = value
            elif arg == "--context":
                context_file = value
            elif arg == "--rebut":
                rebut_file = value
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
        elif arg == "--raw":
            raw = True
            i += 1
        else:
            print(f"error: unexpected argument: {arg}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return 2

    if not provider or not workdir:
        print("error: --provider and --workdir are required", file=sys.stderr)
        print(USAGE, end="", file=sys.stderr)
        return 2

    if question_file:
        question = _read(question_file, "question")
        if question is None:
            return 2
    if not question:
        print("error: --question or --question-file is required", file=sys.stderr)
        return 2

    context = rebut = ""
    if context_file:
        loaded = _read(context_file, "context")
        if loaded is None:
            return 2
        context = loaded
    if rebut_file:
        loaded = _read(rebut_file, "rebut")
        if loaded is None:
            return 2
        rebut = loaded

    provider_list = [p.strip() for p in provider.split(",") if p.strip()]
    for p in provider_list:
        if p not in PROVIDERS:
            print(f"error: unknown provider: {p}", file=sys.stderr)
            return 2
    if len(provider_list) < 2:
        print(f"note: one provider gives one position; independence is the "
              f"point of this mode — pass several, e.g. "
              f"--provider {provider_list[0]},<other>", file=sys.stderr)

    if dry_run:
        print(json.dumps({
            "mode": "discuss",
            "providers": provider_list,
            "question": question[:200],
            "has_context": bool(context),
            "is_rebuttal": bool(rebut),
            "workdir": workdir,
        }, indent=2))
        return 0

    try:
        return _cmd_run(provider_list, workdir, question, context, rebut,
                        timeout, raw)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
