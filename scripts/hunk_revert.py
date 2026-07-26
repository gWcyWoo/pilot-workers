#!/usr/bin/env python3
"""Revert each source hunk of the working diff; the suite must go red.

    python3 scripts/hunk_revert.py            # whole working diff
    HUNK_REVERT_PYTHON=.venv/bin/python python3 scripts/hunk_revert.py

Answers ONE question per hunk: does a test fail when this change is undone? It
is NOT a coverage metric — a hand-picked mutation list has the same blind spot
as the suite it checks, which is why the universe here is derived from the diff
instead of invented. A green hunk means no test depends on it; whether that
matters is a judgement the reader makes.

The universe is mechanically derived — the hunks I actually changed — so unlike a
hand-written mutation list there is nothing to "write all of". It answers exactly
one question per hunk: does a test fail when this change is undone?

Three outcomes, and the middle one matters as much as the first:

  RED     the suite caught it — the hunk has a discriminating test
  GREEN   the suite did not notice — the hunk has NO test that depends on it
  BROKE   reverting one hunk of a multi-hunk change left code that cannot even
          import. Counting that as "caught" would be a lie: no test asserted
          anything, the build simply fell over.

Runs inside a scratch copy only.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COPY = Path(
    os.environ.get("HUNK_REVERT_DIR", tempfile.gettempdir())) / "pw-hunk-revert"
# Absolute: the suite runs with cwd set to the scratch COPY, so a relative
# interpreter path (`.venv/bin/python`) resolves nowhere there. The tool's own
# first run outside the scratchpad hit exactly this.
PYTHON = str(Path(os.environ.get("HUNK_REVERT_PYTHON", sys.executable)).resolve())
DESELECT = ("tests/test_skill_generation.py::"
            "test_upgrade_from_a_markerless_deployment_succeeds")


def sh(args: list[str], cwd: Path, stdin: str | None = None):
    return subprocess.run(args, cwd=cwd, input=stdin, capture_output=True,
                          text=True)


def split_hunks(diff: str) -> list[tuple[str, str]]:
    """(path, single-hunk patch) for every hunk, headers reattached."""
    out: list[tuple[str, str]] = []
    for block in diff.split("\ndiff --git ")[1:]:
        block = "diff --git " + block
        lines = block.splitlines(keepends=True)
        header_end = next(
            (i for i, l in enumerate(lines) if l.startswith("@@")), None)
        if header_end is None:
            continue
        header = "".join(lines[:header_end])
        path_match = re.search(r"^\+\+\+ b/(.+)$", header, re.M)
        if not path_match:
            continue
        path = path_match.group(1)
        current: list[str] = []
        for line in lines[header_end:]:
            if line.startswith("@@") and current:
                out.append((path, header + "".join(current)))
                current = [line]
            else:
                current.append(line)
        if current:
            out.append((path, header + "".join(current)))
    return out


def run_suite() -> str:
    proc = sh([PYTHON, "-m", "pytest", "-x", "-q", "--deselect", DESELECT,
               "-p", "no:randomly"], COPY)
    text = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return "GREEN"
    if ("ERROR" in text or "error" in text.split("=====")[-1]
            or "ImportError" in text or "SyntaxError" in text
            or "NameError while collecting" in text
            or "errors during collection" in text):
        return "BROKE"
    return "RED"


def main() -> int:
    if COPY.exists():
        sh(["rm", "-rf", str(COPY)], COPY.parent)
    COPY.mkdir(parents=True)
    tar = subprocess.Popen(
        ["tar", "cf", "-", "--exclude=.venv", "--exclude=.git",
         "--exclude=__pycache__", "--exclude=*.egg-info", "."],
        cwd=REPO, stdout=subprocess.PIPE)
    subprocess.run(["tar", "xf", "-"], cwd=COPY, stdin=tar.stdout)
    tar.wait()

    diff = sh(["git", "diff", "-U3", "--", "src/"], REPO).stdout
    hunks = split_hunks(diff)
    print(f"source hunks: {len(hunks)}")

    baseline = run_suite()
    print(f"baseline: {baseline}")
    if baseline != "GREEN":
        # Print WHAT failed, not just that something did. A bare "not green"
        # once cost six re-runs that could not reproduce it, so the flake stayed
        # unnamed.
        print("BASELINE NOT GREEN — results would be meaningless")
        proc = sh([PYTHON, "-m", "pytest", "-q", "--deselect", DESELECT,
                   "-p", "no:randomly"], COPY)
        for line in proc.stdout.splitlines():
            if "FAILED" in line or "ERROR" in line or "passed" in line:
                print(f"  {line}")
        return 1

    tally = {"RED": 0, "GREEN": 0, "BROKE": 0, "SKIP": 0}
    survivors: list[str] = []
    for index, (path, patch) in enumerate(hunks, 1):
        target = COPY / path
        if not target.exists():
            tally["SKIP"] += 1
            continue
        original = target.read_bytes()
        applied = sh(["git", "apply", "-R", "--unsafe-paths",
                      f"--directory=.", "-p1", "-"], COPY, stdin=patch)
        if applied.returncode != 0:
            target.write_bytes(original)
            tally["SKIP"] += 1
            continue
        try:
            verdict = run_suite()
        finally:
            target.write_bytes(original)
        tally[verdict] += 1
        first = patch.splitlines()[-1][:70] if patch.splitlines() else ""
        line_no = re.search(r"@@ -(\d+)", patch)
        where = f"{path}:{line_no.group(1) if line_no else '?'}"
        if verdict == "GREEN":
            survivors.append(f"{where}  {first}")
            print(f"[{index}/{len(hunks)}] GREEN  {where}  <-- no test depends on it")
        else:
            print(f"[{index}/{len(hunks)}] {verdict:6} {where}")
        sys.stdout.flush()

    print("\n=== result ===")
    for key in ("RED", "GREEN", "BROKE", "SKIP"):
        print(f"  {key:6}: {tally[key]}")
    graded = tally["RED"] + tally["GREEN"]
    if graded:
        print(f"  discriminating: {tally['RED'] / graded:.0%} of gradable hunks")
    print(f"\n=== hunks with NO discriminating test ({len(survivors)}) ===")
    for line in survivors:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
