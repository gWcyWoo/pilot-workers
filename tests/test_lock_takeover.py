"""A stale-lock takeover must never steal a live lock.

``acquire_run_lock`` unlinks a lock it judged stale. Unguarded, the judgement
and the unlink are two steps: a second acquirer can replace the stale lock
with a fresh live one in between, and the first acquirer then unlinks the
LIVE lock and acquires — two runs sharing one sandbox, each convinced it is
exclusive.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from pilot_workers import runtime


def _stale_lock(root) -> None:
    (root / ".lock").write_text(
        json.dumps({"pid": 999999999, "started_at": "gone"}), encoding="utf-8")


def test_stale_lock_is_taken_over(tmp_path):
    _stale_lock(tmp_path)
    runtime.acquire_run_lock(tmp_path)
    lock = runtime.read_run_lock(tmp_path)
    assert lock["pid"] == os.getpid()


def test_live_lock_is_refused(tmp_path):
    runtime.acquire_run_lock(tmp_path)
    with pytest.raises(RuntimeError, match="still active"):
        runtime.acquire_run_lock(tmp_path)


def test_a_slow_judger_does_not_steal_a_freshly_acquired_live_lock(
        tmp_path, monkeypatch):
    """The decisive interleaving.

    Thread A judges the old lock stale, then stalls before its unlink while
    thread B completes a full takeover (B's lock is now LIVE). A must not
    unlink B's lock and acquire: exactly one winner, the loser raises.
    """
    _stale_lock(tmp_path)
    original = runtime.lock_is_stale
    first_judged = threading.Event()
    winner_done = threading.Event()
    calls = []
    lock = threading.Lock()

    def slow_judge(payload):
        verdict = original(payload)
        with lock:
            calls.append(threading.current_thread().name)
            first = len(calls) == 1
        if first and verdict:
            first_judged.set()
            # Stall between the staleness judgement and the unlink until the
            # other acquirer has fully taken the lock over. Under the fixed
            # implementation the other acquirer is blocked on the guard flock,
            # so this wait simply times out.
            winner_done.wait(timeout=2)
        return verdict

    monkeypatch.setattr(runtime, "lock_is_stale", slow_judge)

    outcomes: dict[str, object] = {}

    def acquire(name: str) -> None:
        try:
            runtime.acquire_run_lock(tmp_path)
            outcomes[name] = "acquired"
        except RuntimeError as exc:
            outcomes[name] = exc

    slow = threading.Thread(target=acquire, args=("slow",), name="slow")
    slow.start()
    assert first_judged.wait(timeout=5)

    fast = threading.Thread(target=acquire, args=("fast",), name="fast")
    fast.start()
    fast.join(timeout=5)
    winner_done.set()
    slow.join(timeout=5)

    results = sorted(
        "acquired" if outcome == "acquired" else "refused"
        for outcome in outcomes.values())
    assert results == ["acquired", "refused"], (
        f"a live lock was stolen: {outcomes}")
    # And the survivor's lock is intact on disk.
    assert runtime.read_run_lock(tmp_path)["pid"] == os.getpid()


def test_a_live_process_this_user_cannot_signal_is_not_stale():
    """`os.kill(pid, 0)` raises PermissionError for a live process owned by
    someone else. A bare `except OSError` read that as dead, so
    lock_is_stale({"pid": 1}) said launchd had exited."""
    import os

    try:
        os.kill(1, 0)
    except PermissionError:
        pass
    except OSError:
        pytest.skip("pid 1 is signalable here; the branch cannot be exercised")
    else:
        pytest.skip("pid 1 is signalable here; the branch cannot be exercised")

    lock = {"pid": 1, "started_at": runtime.process_start_time(1)}
    assert runtime.lock_is_stale(lock) is False


def test_a_dead_pid_is_still_stale():
    """Reverse assertion: the PermissionError branch must not swallow the
    ordinary dead-process case."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert runtime.lock_is_stale({"pid": proc.pid, "started_at": "x"}) is True


def test_an_unavailable_start_time_probe_leaves_the_lock_live(monkeypatch):
    """A timed-out or missing `ps` must be conservative: refuse the takeover
    rather than assume the holder is gone."""
    monkeypatch.setattr(runtime, "process_start_time", lambda pid: None)
    assert runtime.lock_is_stale({"pid": os.getpid(), "started_at": "whatever"}) is False


def test_a_lock_with_no_recorded_start_time_is_not_stale():
    """`acquire_run_lock` writes `started_at: None` when the probe fails at that
    moment. Once the probe recovered, `started_at != None` was True and a LIVE
    sandbox was declared stale — two runs in one sandbox, the one thing this
    lock exists to prevent. "Cannot compare" must never read as "gone"."""
    assert runtime.process_start_time(os.getpid()) is not None, (
        "this platform gives no start time; the branch cannot be exercised")
    assert runtime.lock_is_stale({"pid": os.getpid(), "started_at": None}) is False


def test_a_recorded_start_time_that_differs_is_still_stale():
    """Reverse assertion: the new branch must not swallow pid recycling."""
    assert runtime.lock_is_stale(
        {"pid": os.getpid(), "started_at": "definitely-not-now"}) is True
