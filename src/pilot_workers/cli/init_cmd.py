"""pw9 init — generate a project-level skill file.

Reads the current provider configuration and credential status, renders
a skill markdown with an intent→command mapping table, and writes it to
the project's skill directory (.claude/skills/pw9.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

from pilot_workers import providers
from pilot_workers.runners import get_runner
from pilot_workers import runtime

USAGE = """usage: pw9 init [--target claude|codex|all]

Generate a pw9 skill file in the current project so the AI host knows
what pw9 commands are available and how to call them.

  pw9 init                  # default: claude
  pw9 init --target claude  # .claude/skills/pw9/SKILL.md
  pw9 init --target codex   # .codex/skills/pw9/SKILL.md
  pw9 init --target all     # both
"""

HOSTS = {
    "claude": lambda: Path.cwd() / ".claude",
    "codex": lambda: Path.cwd() / ".codex",
}


def _credential_ok(provider_key: str) -> bool:
    try:
        provider = providers.PROVIDERS[provider_key]
        runner = get_runner(provider.runner)
        meta = runtime.credential_metadata(provider, runner)
        return meta["configured"]
    except (KeyError, OSError, RuntimeError):
        return False


def _render_skill() -> str:
    ready: list[dict] = []
    for key in sorted(providers.PROVIDERS):
        if not _credential_ok(key):
            continue
        provider = providers.PROVIDERS[key]
        modes = [m.strip() for m in provider.suitable_modes.split(",") if m.strip()]
        ready.append({"key": key, "modes": modes, "strengths": provider.strengths})

    if not ready:
        return ""

    lines = [
        "---",
        "name: pw9",
        "description: Worker dispatch tool — use it when asked to review, test, explore, or delegate code tasks to a worker provider.",
        "---",
        "",
        "# pw9 — Available Worker Commands",
        "",
        "This project has `pw9` configured with the following providers.",
        "When the user asks to use a provider by name, or asks for review/test/explore/code",
        "and names a provider, translate the request into the matching pw9 command.",
        "",
        "## Providers",
        "",
        "| Provider | Strengths | Suitable modes |",
        "| --- | --- | --- |",
    ]
    for p in ready:
        lines.append(f"| {p['key']} | {p['strengths']} | {', '.join(p['modes'])} |")

    lines.append("")
    lines.append("## Commands")
    lines.append("")

    for p in ready:
        key = p["key"]
        modes = p["modes"]

        if "review" in modes:
            lines.append(f'- **"review with {key}" / "use {key} to review"**')
            lines.append(f"  ```bash")
            lines.append(f'  pw9 review --provider {key} --workdir "$PWD"')
            lines.append(f"  ```")
            lines.append(f"  Auto-splits into {_axis_count()} axes and runs in parallel. No task file needed.")
            lines.append("")

        if "test" in modes:
            lines.append(f'- **"test with {key}" / "use {key} to run tests"**')
            lines.append(f"  ```bash")
            lines.append(f'  pw9 test --provider {key} --workdir "$PWD"')
            lines.append(f"  ```")
            lines.append(f"  Auto-splits into test layers and runs in parallel. No task file needed.")
            lines.append("")

        if "code" in modes:
            lines.append(f'- **"code with {key}" / "use {key} to implement X"**')
            lines.append(f"  ```bash")
            lines.append(f'  pw9 template code > /tmp/{key}-code-task.md  # fill it in')
            lines.append(f'  pw9 dispatch --provider {key} --mode code --workdir "$PWD" --task-file /tmp/{key}-code-task.md')
            lines.append(f"  ```")
            lines.append(f"  Requires a task file — run `pw9 template code` to generate one, fill it in, then dispatch.")
            lines.append("")

        if "explore" in modes:
            lines.append(f'- **"explore with {key}" / "use {key} to investigate X"**')
            lines.append(f"  ```bash")
            lines.append(f'  pw9 explore --provider {key} --workdir "$PWD" --requirement "describe what to explore"')
            lines.append(f"  ```")
            lines.append(f"  Auto-splits into {_lens_count()} lenses (flow/constraints/impact/abstraction) and runs in parallel. Also supports `--requirement-file <path>`.")
            lines.append("")

    if len(ready) > 1:
        keys = ",".join(p["key"] for p in ready)
        lines.append('- **"what do the models think about X" / "get a second opinion"**')
        lines.append(f"  ```bash")
        lines.append(f'  pw9 discuss --provider {keys} --workdir "$PWD" --question "..."')
        lines.append(f"  ```")
        lines.append(f"  Each provider answers independently and cannot see the others."
                     f" The output is their positions and where they SPLIT — not a merged")
        lines.append(f"  conclusion. You decide; run another round with `--rebut <file>`"
                     f" if it is worth it.")
        lines.append("")

    lines.append("## When NOT to dispatch")
    lines.append("")
    lines.append("Delegating is not free: a worker cannot see this conversation, so")
    lines.append("everything it needs must be written down, and its output must be read")
    lines.append("back and judged. Keep the work here when:")
    lines.append("")
    lines.append("- **The spec would be longer than the diff.** If describing the change")
    lines.append("  precisely enough for a worker takes more effort than making it, make it.")
    lines.append("- **The check is faster than the round trip.** A suite that finishes in")
    lines.append("  under a minute, a one-line grep, a file you already have open — do it")
    lines.append("  directly. A dispatch costs a process start, a model call, and a verdict")
    lines.append("  to parse.")
    lines.append("- **The answer needs this conversation.** Anything depending on what the")
    lines.append("  user just said, on a decision made three turns ago, or on unwritten")
    lines.append("  context cannot be delegated — the worker starts blank.")
    lines.append("- **Judgment is the deliverable.** Which approach to take, what the")
    lines.append("  requirement really means, whether a trade-off is acceptable: decide")
    lines.append("  those here, then delegate the execution.")
    lines.append("")
    lines.append("The inverse is where dispatch pays: reading a lot to answer a little")
    lines.append("(`explore`), the same scope examined from several angles at once")
    lines.append("(`review`), and bulk execution against a spec you have already settled")
    lines.append("(`code`). Reading is where the tokens go — that is the work worth moving.")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- `review`, `test`, and `explore` run in the foreground and print a JSON verdict array to stdout.")
    lines.append(f"- Default per-job budget is {_default_timeout()}s, and these commands run in the")
    lines.append("  FOREGROUND. Your shell tool will usually cut off long before that and kill the")
    lines.append("  fanout mid-flight. Run them in a background shell with `--out <path>`, then read")
    lines.append("  the file when the shell exits:")
    lines.append("  ```bash")
    lines.append('  pw9 review --provider <key> --workdir "$PWD" --out /tmp/pw9-review.json')
    lines.append("  ```")
    lines.append("- After a `review`, findings are merged by location; `[Nx]` means N independent")
    lines.append("  models flagged the same place. `--replicate` gives every axis to every")
    lines.append("  provider, which is what makes that number meaningful.")
    lines.append("- `dispatch` prints two JSON lines (started + verdict) — run in a background Bash.")
    lines.append("- `--provider` accepts comma-separated keys for cross-model dispatch: `--provider ds,glm,kimi-k3` round-robins across providers.")
    lines.append("- All commands run against the current workdir. Workers cannot see this conversation.")
    lines.append("- Never put credentials in a task file.")
    lines.append("")

    return "\n".join(lines) + "\n"


def _axis_count() -> int:
    from pilot_workers import strategies
    return len(strategies.effective("review"))


def _lens_count() -> int:
    from pilot_workers import strategies
    return len(strategies.effective("explore"))


def _default_timeout() -> int:
    """Read from the CLI's own default so the skill text cannot drift."""
    from pilot_workers.cli import review_cmd

    return review_cmd.DEFAULT_TIMEOUT_S


def _write_skill(host: str, content: str) -> None:
    base = HOSTS[host]()
    skill_dir = base / "skills" / "pw9"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    print(f"  wrote {skill_path}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    target = "claude"
    i = 0
    while i < len(args):
        if args[i] == "--target" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        else:
            print(f"error: unexpected argument: {args[i]}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return 2

    if target not in (*HOSTS, "all"):
        print(f"error: unknown target: {target} (expected claude, codex, or all)",
              file=sys.stderr)
        return 2

    content = _render_skill()
    if not content:
        print("error: no providers have API keys configured; run 'pw9 key <provider>' first",
              file=sys.stderr)
        return 1

    hosts = list(HOSTS) if target == "all" else [target]
    for host in hosts:
        _write_skill(host, content)
    print(f"  {_provider_count()} providers, ready to use")
    return 0


def _provider_count() -> int:
    return sum(1 for k in providers.PROVIDERS if _credential_ok(k))
