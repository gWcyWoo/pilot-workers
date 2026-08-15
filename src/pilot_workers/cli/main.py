#!/usr/bin/env python3
"""Unified CLI entry: pilot-workers <subcommand> ..."""

from __future__ import annotations

from pathlib import Path
import sys


USAGE = """usage: pilot-workers <subcommand> [args]

subcommands:
  run          Dispatch a bounded task to an isolated LLM worker.
  dispatch     Deterministic outer shell around run (started + verdict JSON).
  fanout       Dispatch several jobs concurrently; stdout = one JSON array of verdicts.
  template     Print the task template for a mode (code|explore|test|review).
  init         Generate a pw9 skill file in the current project.
  install      Install the worker runtime (pw9 install runner opencode).
  uninstall    Remove a runner or API key.
  key          Configure a provider's API key (pw9 key <provider>).
  status       Provider API keys, runner state [--json].
  runs         Dispatch history, newest first [--provider|--mode|--since|--last].
  usage        Token totals per provider [--since <N>d].
  permissions  Per-provider permission overrides: add/remove/show rules.
  provider     Manage provider configurations (add/edit/remove/show).
  explore      Auto-fanout exploration by lenses (flow/constraints/impact/abstraction).
  review       Auto-fanout code review by configured axes (add/edit/remove/show).
  test         Auto-fanout test execution by configured layers (add/edit/remove/show).
  maintain     Worker log, sandbox and worktree lifecycle tools.
  runtime      Deprecated alias for 'install runner opencode'.

install forms:
  install runner <name>

uninstall forms:
  uninstall runner <name>
  uninstall key <provider>

Use 'pilot-workers <subcommand> --help' for subcommand-specific help.
"""


def _with_argv(program: str, rest: list[str], fn) -> int:
    """Call a no-arg main() that parses sys.argv, with a temporary argv."""
    original = sys.argv
    sys.argv = [program] + rest
    try:
        return fn()
    finally:
        sys.argv = original


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0

    subcommand, rest = args[0], args[1:]

    if subcommand == "run":
        from pilot_workers.cli.run import main as run_main

        return run_main(rest)

    if subcommand == "dispatch":
        from pilot_workers.cli.dispatch import main as dispatch_main

        return dispatch_main(rest)

    if subcommand == "fanout":
        from pilot_workers.cli.fanout import main as fanout_main

        return fanout_main(rest)

    if subcommand == "template":
        import pilot_workers

        modes = ("code", "explore", "test", "review")
        if rest and rest[0] in ("-h", "--help"):
            print(f"usage: pilot-workers template {{{'|'.join(modes)}}}")
            return 0
        if len(rest) != 1 or rest[0] not in modes:
            print(f"usage: pilot-workers template {{{'|'.join(modes)}}}", file=sys.stderr)
            return 2
        path = (
            Path(pilot_workers.__file__).resolve().parent
            / "data" / "templates" / f"{rest[0]}.md"
        )
        if not path.is_file():
            print(f"error: template missing from package: {path}", file=sys.stderr)
            return 1
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0

    if subcommand == "init":
        from pilot_workers.cli.init_cmd import main as init_main

        return init_main(rest)

    if subcommand == "install":
        from pilot_workers.cli.install import main as install_main

        return install_main(rest)

    if subcommand == "uninstall":
        from pilot_workers.cli import install as install_mod

        fn = getattr(install_mod, "uninstall_main", None)
        if fn is None:
            print("error: uninstall not available in this build", file=sys.stderr)
            return 1
        return fn(rest)

    if subcommand == "key":
        from pilot_workers.cli.install import key_main

        return key_main(rest)

    if subcommand == "status":
        from pilot_workers.cli.status import main as status_main

        return status_main(rest)

    if subcommand == "permissions":
        from pilot_workers.cli.permissions import main as permissions_main

        return permissions_main(rest)

    if subcommand in ("runs", "usage"):
        from pilot_workers.cli.runs_cmd import main as runs_main

        return runs_main(rest, command=subcommand)

    if subcommand == "provider":
        from pilot_workers.cli.provider_cmd import main as provider_main

        return provider_main(rest)

    if subcommand == "explore":
        from pilot_workers.cli.explore_cmd import main as explore_main

        return explore_main(rest)

    if subcommand == "review":
        from pilot_workers.cli.review_cmd import main as review_main

        return review_main(rest)

    if subcommand == "test":
        from pilot_workers.cli.test_cmd import main as test_main

        return test_main(rest)

    if subcommand == "maintain":
        from pilot_workers.maintain import main as maintain_main

        return _with_argv("pilot-workers maintain", rest, maintain_main)

    if subcommand == "runtime":
        if rest != ["install"]:
            print("usage: pilot-workers runtime install", file=sys.stderr)
            return 2
        print(
            "note: 'runtime install' is deprecated; "
            "use 'pilot-workers install runner opencode'",
            file=sys.stderr,
        )
        from pilot_workers.cli.install import main as install_main

        return install_main(["runner", "opencode"])

    print(f"error: unknown subcommand: {subcommand}", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
