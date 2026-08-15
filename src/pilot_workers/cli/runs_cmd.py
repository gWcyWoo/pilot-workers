"""pw9 runs / pw9 usage — read the dispatch history back.

Every dispatch already writes a ``<stem>.verdict.json`` beside its log, with
tokens, duration, mode and outcome. Nothing ever read them again, so the one
thing this tool exists to do — move read-heavy work onto cheap workers — was
invisible after the fact. These commands are pure read-only aggregation over
those artifacts: no new bookkeeping, nothing to keep in sync.

What is deliberately NOT reported: an estimate of "tokens you saved". The
counterfactual (what the planner would have spent doing it itself) is not
measured anywhere, and a made-up number here would discredit the real ones.
The honest line is what the workers actually consumed.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from pilot_workers import providers

USAGE = """usage:
  pw9 runs  [--provider <key>] [--mode <mode>] [--since <N>d|<N>h] [--last <N>] [--json]
  pw9 usage [--provider <key>] [--since <N>d|<N>h] [--json]

runs   one line per dispatch, newest first
usage  token totals per provider

Both read the verdict files each dispatch already wrote; they never run a
worker and never change anything on disk.
"""


def _logs_root() -> Path:
    return providers.workers_root() / "logs"


def _parse_since(text: str) -> float | None:
    """`7d` / `12h` -> a unix cutoff. Returns None when unparseable."""
    text = text.strip().lower()
    if not text or text[-1] not in ("d", "h"):
        return None
    try:
        amount = float(text[:-1])
    except ValueError:
        return None
    seconds = amount * (86400 if text[-1] == "d" else 3600)
    return time.time() - seconds


def _iter_verdicts() -> Iterator[tuple[Path, dict[str, Any]]]:
    root = _logs_root()
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/*.verdict.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # One corrupt or unreadable artifact must not take the whole
            # history down: the ledger is a convenience, not a contract.
            continue
        if isinstance(data, dict):
            yield path, data


def _collect(provider: str | None = None, mode: str | None = None,
             since: float | None = None) -> list[dict[str, Any]]:
    """Newest-first verdicts, one per run.

    A resumed run writes a second artifact whose stem carries the same
    sandbox id with a new attempt id (`<sandbox>+<attempt>`), and both
    describe ONE run — counting them twice would inflate exactly the number
    this command exists to report honestly. The newest attempt wins.
    """
    by_run: dict[str, tuple[float, dict[str, Any]]] = {}
    for path, data in _iter_verdicts():
        if provider and data.get("provider") != provider:
            continue
        if mode and data.get("mode") != mode:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if since is not None and mtime < since:
            continue
        # Fall back to the artifact stem when a verdict predates run_id.
        run_id = str(data.get("run_id") or path.stem.partition("+")[0])
        data = dict(data)
        data["_mtime"] = mtime
        previous = by_run.get(run_id)
        if previous is None or mtime > previous[0]:
            by_run[run_id] = (mtime, data)
    rows = [d for _, d in by_run.values()]
    rows.sort(key=lambda d: d["_mtime"], reverse=True)
    return rows


def _tokens(row: dict[str, Any]) -> dict[str, int]:
    raw = row.get("tokens")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, int)}


def _human_int(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _cmd_runs(provider, mode, since, last, json_mode) -> int:
    rows = _collect(provider, mode, since)
    if last is not None:
        rows = rows[:last]
    if json_mode:
        for row in rows:
            row.pop("_mtime", None)
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(no runs recorded)")
        return 0
    table = []
    for row in rows:
        tk = _tokens(row)
        total = tk.get("input", 0) + tk.get("output", 0) + tk.get("reasoning", 0)
        table.append([
            time.strftime("%m-%d %H:%M", time.localtime(row["_mtime"])),
            str(row.get("provider") or "-"),
            str(row.get("mode") or "-"),
            str(row.get("verdict") or "-"),
            str(row.get("steps") or "-"),
            _human_int(total),
            f"{row.get('duration_s') or 0}s",
        ])
    print(_render_table(
        ["WHEN", "PROVIDER", "MODE", "VERDICT", "STEPS", "TOKENS", "TIME"], table))
    print(f"\n{len(rows)} runs")
    return 0


def _cmd_usage(provider, since, json_mode) -> int:
    rows = _collect(provider, None, since)
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        key = str(row.get("provider") or "-")
        bucket = totals.setdefault(
            key, {"runs": 0, "input": 0, "output": 0, "reasoning": 0,
                  "cache_read": 0, "duration_s": 0})
        bucket["runs"] += 1
        bucket["duration_s"] += int(row.get("duration_s") or 0)
        for field, value in _tokens(row).items():
            if field in bucket:
                bucket[field] += value
    if json_mode:
        print(json.dumps(totals, indent=2))
        return 0
    if not totals:
        print("(no runs recorded)")
        return 0
    table = []
    for key in sorted(totals):
        b = totals[key]
        table.append([
            key, str(b["runs"]),
            _human_int(b["input"]), _human_int(b["output"]),
            _human_int(b["reasoning"]), _human_int(b["cache_read"]),
            f"{b['duration_s'] // 60}m",
        ])
    print(_render_table(
        ["PROVIDER", "RUNS", "IN", "OUT", "REASONING", "CACHE-READ", "TIME"],
        table))
    grand = sum(b["input"] + b["output"] + b["reasoning"] for b in totals.values())
    print(f"\n{_human_int(grand)} tokens across {sum(b['runs'] for b in totals.values())} "
          f"runs — read by workers, not by this session.")
    return 0


def _parse(args: list[str]) -> dict[str, Any] | None:
    opts: dict[str, Any] = {"provider": None, "mode": None, "since": None,
                            "last": None, "json": False}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--json":
            opts["json"] = True
            i += 1
        elif arg in ("--provider", "--mode", "--since", "--last") and i + 1 < len(args):
            value = args[i + 1]
            if arg == "--last":
                try:
                    opts["last"] = int(value)
                except ValueError:
                    print(f"error: --last requires an integer, got {value!r}",
                          file=sys.stderr)
                    return None
            elif arg == "--since":
                cutoff = _parse_since(value)
                if cutoff is None:
                    print(f"error: --since expects <N>d or <N>h, got {value!r}",
                          file=sys.stderr)
                    return None
                opts["since"] = cutoff
            else:
                opts[arg[2:]] = value
            i += 2
        else:
            print(f"error: unexpected argument: {arg}", file=sys.stderr)
            print(USAGE, end="", file=sys.stderr)
            return None
    return opts


def main(argv: list[str] | None = None, *, command: str = "runs") -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0
    opts = _parse(args)
    if opts is None:
        return 2
    if opts["provider"] and opts["provider"] not in providers.PROVIDERS:
        print(f"note: {opts['provider']!r} is not a configured provider; "
              f"showing its history anyway", file=sys.stderr)
    try:
        if command == "usage":
            return _cmd_usage(opts["provider"], opts["since"], opts["json"])
        return _cmd_runs(opts["provider"], opts["mode"], opts["since"],
                         opts["last"], opts["json"])
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
