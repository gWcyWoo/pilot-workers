"""RED tests for pilot-workers v0.5.0 D4 fanout hardening.

Watchdog behavior is tested end-to-end through ``run_fanout`` with stub
"dispatch child" scripts (tiny python files under tmp_path);
``_build_dispatch_command`` is monkeypatched to run them, following the
conventions of tests/test_fanout.py. Timing constants
(``runtime.TERMINATE_GRACE_SECONDS``, ``fanout.HARVEST_ALLOWANCE_SECONDS``,
``fanout.MAX_EPILOGUE_SECONDS``) are monkeypatched to tiny values so total
wall time stays low. Stub children always exit on their own after a bounded
sleep, so a missing watchdog can never hang the suite.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import threading
import time

import pytest

from pilot_workers import runtime
from pilot_workers.cli import dispatch as dispatch_mod
from pilot_workers.cli import fanout as fanout_mod


# ---------------------------------------------------------------------------
# Stub child scripts (run as [sys.executable, script, ...])
# ---------------------------------------------------------------------------

# Prints `started`, then goes fully silent for `sleep_s`, then writes a
# marker file. If the watchdog works, the marker is never written.
_SILENT_CHILD = r"""
import json
import sys
import time

sleep_s = float(sys.argv[1])
marker = sys.argv[2]
sys.stdout.write(json.dumps({
    "type": "worker_runner.started", "provider": "glm", "run_id": "r1",
}) + "\n")
sys.stdout.flush()
time.sleep(sleep_s)
with open(marker, "w", encoding="utf-8") as handle:
    handle.write("finished")
"""

# Prints `started`, sleeps briefly, then emits a real v2 verdict and exits.
_QUICK_CHILD = r"""
import json
import sys
import time

sys.stdout.write(json.dumps({
    "type": "worker_runner.started", "provider": "glm", "run_id": "r1",
}) + "\n")
sys.stdout.flush()
time.sleep(0.5)
sys.stdout.write(json.dumps({
    "type": "worker_runner.verdict", "schema_version": 2,
    "verdict": "completed", "exit_code": 0,
    "timed_out": False, "idle_timed_out": False, "interrupted": False,
}) + "\n")
sys.stdout.flush()
"""

# Prints `started` and one real stderr line, then keeps emitting heartbeat
# lines on stderr. Writes `alive_marker` at `alive_at` seconds (proving it
# survived past the plain deadline+grace kill point) and `finish_marker`
# after `end_at` seconds (proving it was NOT killed before then).
_HEARTBEAT_CHILD = r"""
import json
import sys
import time

alive_at = float(sys.argv[1])
end_at = float(sys.argv[2])
alive_marker = sys.argv[3]
finish_marker = sys.argv[4]
sys.stdout.write(json.dumps({
    "type": "worker_runner.started", "provider": "glm", "run_id": "r1",
}) + "\n")
sys.stdout.flush()
sys.stderr.write("real-error-output\n")
sys.stderr.flush()
start = time.monotonic()
wrote_alive = False
while time.monotonic() - start < end_at:
    if not wrote_alive and time.monotonic() - start >= alive_at:
        with open(alive_marker, "w", encoding="utf-8") as handle:
            handle.write("alive")
        wrote_alive = True
    sys.stderr.write("pilot-workers-heartbeat harvesting\n")
    sys.stderr.flush()
    time.sleep(0.1)
with open(finish_marker, "w", encoding="utf-8") as handle:
    handle.write("finished")
"""

# Emits a real v2 verdict immediately, then sleeps silently. The watchdog
# should still kill it (no activity past the deadline), but the captured
# verdict must win over the recorded reason.
_VERDICT_THEN_SLEEP_CHILD = r"""
import json
import sys
import time

sleep_s = float(sys.argv[1])
marker = sys.argv[2]
sys.stdout.write(json.dumps({
    "type": "worker_runner.started", "provider": "glm", "run_id": "r1",
}) + "\n")
sys.stdout.write(json.dumps({
    "type": "worker_runner.verdict", "schema_version": 2,
    "verdict": "completed", "exit_code": 0,
    "timed_out": False, "idle_timed_out": False, "interrupted": False,
}) + "\n")
sys.stdout.flush()
time.sleep(sleep_s)
with open(marker, "w", encoding="utf-8") as handle:
    handle.write("finished")
"""

# Emits a real v2 verdict with verdict "completed" but one sibling flag
# (argv[1]) set to true.
_FLAG_CHILD = r"""
import json
import sys

flag = sys.argv[1]
sys.stdout.write(json.dumps({
    "type": "worker_runner.started", "provider": "glm", "run_id": "r1",
}) + "\n")
verdict = {
    "type": "worker_runner.verdict", "schema_version": 2,
    "verdict": "completed", "exit_code": 0,
    "timed_out": False, "idle_timed_out": False, "interrupted": False,
}
verdict[flag] = True
sys.stdout.write(json.dumps(verdict) + "\n")
sys.stdout.flush()
"""


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _allow_credentials(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": True},
    )


@pytest.fixture
def task_file(tmp_path):
    path = tmp_path / "task.md"
    path.write_text("# task\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def tiny_constants(monkeypatch):
    monkeypatch.setattr(runtime, "TERMINATE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(
        fanout_mod, "HARVEST_ALLOWANCE_SECONDS", 0.3, raising=False
    )
    monkeypatch.setattr(
        fanout_mod, "MAX_EPILOGUE_SECONDS", 3.0, raising=False
    )


def _write_stub(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return str(path)


def _patch_dispatch_command(monkeypatch, script, *script_args):
    monkeypatch.setattr(
        fanout_mod,
        "_build_dispatch_command",
        lambda job, workdir, timeout, idle_timeout: [
            sys.executable, script, *script_args,
        ],
    )


# ---------------------------------------------------------------------------
# Per-job watchdog
# ---------------------------------------------------------------------------


def test_watchdog_kills_silent_child_after_deadline(
    monkeypatch, capsys, tmp_path, task_file, workdir, tiny_constants
):
    _allow_credentials(monkeypatch)
    marker = tmp_path / "finished.marker"
    script = _write_stub(tmp_path, "silent_child.py", _SILENT_CHILD)
    # Deadline = 1 + 0.2 + 0.3 = 1.5s; the child would sleep 5s if let be.
    _patch_dispatch_command(monkeypatch, script, "5", str(marker))
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
        "--timeout", "1",
    ])
    assert rc == 1
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    verdict = results[0]
    assert verdict["synthesized"] is True
    assert verdict["reason"] == "timeout"
    assert verdict["schema_version"] == 2
    assert not marker.exists(), "child should have been killed before finishing"


def test_timeout_zero_disables_watchdog(
    monkeypatch, capsys, tmp_path, task_file, workdir, tiny_constants
):
    _allow_credentials(monkeypatch)
    script = _write_stub(tmp_path, "quick_child.py", _QUICK_CHILD)
    _patch_dispatch_command(monkeypatch, script)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
        "--timeout", "0",
    ])
    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert len(results) == 1
    assert results[0]["verdict"] == "completed"
    assert "synthesized" not in results[0]


def test_heartbeats_delay_kill_until_epilogue_ceiling(
    monkeypatch, capsys, tmp_path, task_file, workdir, tiny_constants
):
    _allow_credentials(monkeypatch)
    alive = tmp_path / "alive.marker"
    finished = tmp_path / "finished.marker"
    script = _write_stub(tmp_path, "heartbeat_child.py", _HEARTBEAT_CHILD)
    # Deadline = 1.5s; plain silence kill would land ~1.7s. alive_at=3.0s
    # sits past that; the MAX_EPILOGUE ceiling lands at deadline + 3.0 = 4.5s,
    # well before the natural end at 8.0s.
    _patch_dispatch_command(
        monkeypatch, script, "3.0", "8.0", str(alive), str(finished)
    )
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
        "--timeout", "1",
    ])
    assert rc == 1
    results = json.loads(capsys.readouterr().out)
    verdict = results[0]
    assert verdict["synthesized"] is True
    assert verdict["reason"] == "timeout"
    assert alive.exists(), "heartbeat activity should delay the silence kill"
    assert not finished.exists(), "epilogue ceiling should kill despite heartbeats"
    # Heartbeat lines are filtered out of stderr_tail.
    assert "pilot-workers-heartbeat" not in verdict["stderr_tail"]
    assert "real-error-output" in verdict["stderr_tail"]


def test_real_verdict_wins_over_recorded_reason(
    monkeypatch, capsys, tmp_path, task_file, workdir, tiny_constants
):
    _allow_credentials(monkeypatch)
    marker = tmp_path / "finished.marker"
    script = _write_stub(
        tmp_path, "verdict_then_sleep.py", _VERDICT_THEN_SLEEP_CHILD
    )
    _patch_dispatch_command(monkeypatch, script, "3", str(marker))
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
        "--timeout", "1",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    results = json.loads(captured.out)
    verdict = results[0]
    # The captured real verdict is authoritative...
    assert verdict["verdict"] == "completed"
    assert "synthesized" not in verdict
    assert "reason" not in verdict
    # ...even though the watchdog killed the still-running silent child...
    assert not marker.exists(), "watchdog should still kill the silent child"
    # ...and the recorded reason is logged to stderr only.
    assert "timeout" in captured.err


# ---------------------------------------------------------------------------
# Exit-code contract: sibling flags veto success
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["timed_out", "idle_timed_out", "interrupted"]
)
def test_completed_verdict_with_true_flag_exits_1(
    monkeypatch, capsys, tmp_path, task_file, workdir, flag
):
    _allow_credentials(monkeypatch)
    script = _write_stub(tmp_path, "flag_child.py", _FLAG_CHILD)
    _patch_dispatch_command(monkeypatch, script, flag)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
        "--timeout", "0",
    ])
    assert rc == 1
    results = json.loads(capsys.readouterr().out)
    assert results[0]["verdict"] == "completed"
    assert results[0][flag] is True
    assert "synthesized" not in results[0]


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, pid, running):
        self.pid = pid
        self.running = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_install_signal_handlers_marks_reasons_before_killing(monkeypatch):
    monkeypatch.setattr(runtime, "TERMINATE_GRACE_SECONDS", 0.05)
    reasons = [None, None]
    killpg_snapshots = []

    def fake_killpg(pgid, sig):
        killpg_snapshots.append((pgid, sig, list(reasons)))

    monkeypatch.setattr(os, "killpg", fake_killpg)
    procs = [_FakeProc(111, running=True), _FakeProc(222, running=False)]
    previous = {
        signal.SIGINT: signal.getsignal(signal.SIGINT),
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
    }
    try:
        fanout_mod._install_signal_handlers(procs, reasons)
        sigint_handler = signal.getsignal(signal.SIGINT)
        sigterm_handler = signal.getsignal(signal.SIGTERM)
        assert callable(sigint_handler)
        assert sigint_handler is not previous[signal.SIGINT]
        assert callable(sigterm_handler)
        assert sigterm_handler not in (signal.SIG_DFL, signal.SIG_IGN)
        # Invoke the SIGTERM handler in-process (killpg is faked): it must
        # return, mark running jobs interrupted BEFORE any kill, and never
        # touch the already-finished job.
        sigterm_handler(signal.SIGTERM, None)
        assert reasons == ["interrupted", None]
        assert killpg_snapshots, "handler must killpg the running child group"
        assert all(pgid == 111 for pgid, _, _ in killpg_snapshots)
        assert all(
            snapshot[0] == "interrupted" for _, _, snapshot in killpg_snapshots
        ), "reason must be recorded before any kill"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# ---------------------------------------------------------------------------
# Dispatch epilogue heartbeat (API pinned for D4)
# ---------------------------------------------------------------------------


def test_heartbeat_line_prefix_constant():
    assert dispatch_mod.HEARTBEAT_LINE_PREFIX == "pilot-workers-heartbeat"


def test_epilogue_heartbeat_emits_until_stopped(monkeypatch, capsys):
    monkeypatch.setattr(
        dispatch_mod, "EPILOGUE_HEARTBEAT_SECONDS", 0.05, raising=False
    )
    stop = dispatch_mod.start_epilogue_heartbeat()
    try:
        assert isinstance(stop, threading.Event)
        assert not stop.is_set()
        time.sleep(0.2)
    finally:
        stop.set()
    time.sleep(0.15)
    err = capsys.readouterr().err
    expected = f"{dispatch_mod.HEARTBEAT_LINE_PREFIX} harvesting"
    assert expected in err
    time.sleep(0.2)
    assert capsys.readouterr().err == "", (
        "heartbeat must stop once the returned event is set"
    )
