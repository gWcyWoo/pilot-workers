"""The read-side step cap in runtime.run_process.

`STEPS_BY_MODE` is a promise the verdict reports against, and until now it was
kept entirely by the engine (OpenCode's `steps` option). Claude Code's CLI has
no turn limit at all — its agent `maxTurns` applies to subagents only — so the
cap has to be enforceable from outside the engine, or it is not a cap.
"""

from __future__ import annotations

import sys

from pilot_workers import runtime
from pilot_workers.runners.base import UnifiedEvent


class _StepEmitter:
    """Minimal stand-in for a Runner: run_process only calls parse_events."""

    def parse_events(self, raw: dict) -> list[UnifiedEvent]:
        if raw.get("type") == "step":
            return [UnifiedEvent(kind="step")]
        return []


# A child that would never stop on its own. If the cap does not fire, the test
# hangs until the wall-clock timeout — which is the failure it is written to
# catch.
_ENDLESS = (
    "import json,sys,time\n"
    "for i in range(10000):\n"
    "    sys.stdout.write(json.dumps({'type':'step','i':i})+'\\n')\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.05)\n"
)


def _run(tmp_path, *, max_steps: int, timeout_s: int = 30):
    return runtime.run_process(
        [sys.executable, "-c", _ENDLESS],
        env={"PATH": "/usr/bin:/bin"},
        task="",
        log_path=tmp_path / "out.jsonl",
        stderr_path=tmp_path / "err.log",
        secret="",
        runner=_StepEmitter(),
        timeout_s=timeout_s,
        max_steps=max_steps,
    )


def test_a_run_that_reaches_its_step_cap_is_terminated(tmp_path):
    result = _run(tmp_path, max_steps=5)
    assert result.step_capped is True
    assert result.timed_out is False
    assert result.idle_timed_out is False


def test_a_capped_run_never_reports_success(tmp_path):
    """`dispatch.classify_verdict` reads the exit code; a worker killed part
    way through has not completed, whatever it managed to print."""
    assert _run(tmp_path, max_steps=3).exit_code != 0


def test_the_cap_stops_the_child_near_the_limit_not_at_the_end(tmp_path):
    """The child would emit 10000 steps. Killing it at the cap is the point;
    the count may overshoot slightly because the reader thread and the poll
    loop are not synchronous, but it must not run away."""
    result = _run(tmp_path, max_steps=4)
    emitted = len((tmp_path / "out.jsonl").read_text().strip().splitlines())
    assert result.step_capped is True
    assert emitted < 100


def test_zero_disables_the_cap_like_the_timeouts(tmp_path):
    """A run that ends on its own with max_steps=0 must not be flagged."""
    result = runtime.run_process(
        [sys.executable, "-c",
         "import json,sys;sys.stdout.write(json.dumps({'type':'step'})+'\\n')"],
        env={"PATH": "/usr/bin:/bin"},
        task="",
        log_path=tmp_path / "out.jsonl",
        stderr_path=tmp_path / "err.log",
        secret="",
        runner=_StepEmitter(),
        max_steps=0,
    )
    assert result.step_capped is False
    assert result.exit_code == 0
