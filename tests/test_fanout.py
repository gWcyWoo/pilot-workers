"""Offline tests for pilot_workers.cli.fanout."""

from __future__ import annotations

import io
import json

import pytest

from pilot_workers import runtime
from pilot_workers.cli import fanout as fanout_mod


def _allow_credentials(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": True},
    )


def _deny_credentials(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "credential_metadata",
        lambda provider, runner: {"configured": False, "secure_mode": False},
    )


class _FakePopen:
    """Minimal Popen stand-in: canned stdout lines, stderr text, returncode."""

    def __init__(self, cmd, behavior):
        self.cmd = cmd
        lines = behavior.get("stdout_lines", [])
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.stderr = io.StringIO(behavior.get("stderr", ""))
        self._rc = behavior.get("returncode", 0)
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def poll(self):
        return self.returncode


def _install_fake_popen(monkeypatch, behavior_for):
    """Patch subprocess.Popen; behavior_for(cmd) -> behavior dict.

    Returns the list of commands seen (thread-safe enough for tests).
    """
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return _FakePopen(cmd, behavior_for(cmd))

    monkeypatch.setattr(fanout_mod.subprocess, "Popen", fake_popen)
    return calls


def _provider_of(cmd):
    return cmd[cmd.index("--provider") + 1]


def _verdict_line(verdict, provider="glm"):
    return json.dumps({
        "type": "worker_runner.verdict",
        "provider": provider,
        "verdict": verdict,
        "exit_code": 0,
    })


def _started_line(provider="glm"):
    return json.dumps({
        "type": "worker_runner.started",
        "provider": provider,
        "run_id": "r1",
    })


def _ok_behavior(cmd):
    provider = _provider_of(cmd)
    return {
        "stdout_lines": [_started_line(provider), _verdict_line("completed", provider)],
        "returncode": 0,
    }


@pytest.fixture
def task_file(tmp_path):
    path = tmp_path / "task.md"
    path.write_text("# task\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def test_job_spec_parses(monkeypatch, capsys, tmp_path, task_file, workdir):
    _allow_credentials(monkeypatch)
    _install_fake_popen(monkeypatch, _ok_behavior)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
    ])
    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["job"] for r in results] == ["glm:review"]


def test_job_spec_path_with_colons_parses_via_left_split(
    monkeypatch, capsys, tmp_path, workdir
):
    colon_path = tmp_path / "a:b.md"
    colon_path.write_text("# task\n", encoding="utf-8")
    _allow_credentials(monkeypatch)
    calls = _install_fake_popen(monkeypatch, _ok_behavior)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{colon_path}",
    ])
    assert rc == 0
    assert calls[0][calls[0].index("--task-file") + 1] == str(colon_path)


def test_unknown_provider_exits_2(capsys, task_file, workdir):
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"bogus:review:{task_file}",
    ])
    assert rc == 2
    assert "unknown provider" in capsys.readouterr().err


def test_resume_mode_rejected(capsys, task_file, workdir):
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:resume:{task_file}",
    ])
    assert rc == 2
    assert "resume is not supported in fanout" in capsys.readouterr().err


def test_no_jobs_exits_2(capsys, workdir):
    rc = fanout_mod.main(["--workdir", workdir])
    assert rc == 2


def test_providers_shorthand_expands(monkeypatch, capsys, task_file, workdir):
    _allow_credentials(monkeypatch)
    calls = _install_fake_popen(monkeypatch, _ok_behavior)
    rc = fanout_mod.main([
        "--workdir", workdir,
        "--providers", "kimi-k3,glm",
        "--mode", "review",
        "--task-file", task_file,
    ])
    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["job"] for r in results] == ["kimi-k3:review", "glm:review"]
    assert len(calls) == 2


def test_job_and_jobs_file_together_exits_2(capsys, tmp_path, task_file, workdir):
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text("[]", encoding="utf-8")
    rc = fanout_mod.main([
        "--workdir", workdir,
        "--job", f"glm:review:{task_file}",
        "--jobs-file", str(jobs_file),
    ])
    assert rc == 2


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def test_credential_preflight_failure_spawns_nothing(
    monkeypatch, capsys, task_file, workdir
):
    _deny_credentials(monkeypatch)

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(fanout_mod.subprocess, "Popen", forbidden_popen)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "credential missing for glm" in err
    assert "pilot-workers credentials glm" in err


def _write_jobs_file(tmp_path, jobs):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(jobs), encoding="utf-8")
    return str(path)


def test_code_jobs_same_workdir_without_worktree_exits_2(
    monkeypatch, capsys, tmp_path, task_file, workdir
):
    _allow_credentials(monkeypatch)
    _install_fake_popen(monkeypatch, _ok_behavior)
    jobs_file = _write_jobs_file(tmp_path, [
        {"provider": "glm", "mode": "code", "task_file": task_file},
        {"provider": "kimi-k3", "mode": "code", "task_file": task_file},
    ])
    rc = fanout_mod.main(["--workdir", workdir, "--jobs-file", jobs_file])
    assert rc == 2
    assert "multiple code jobs share one workdir" in capsys.readouterr().err


def test_code_jobs_with_worktree_proceed(
    monkeypatch, capsys, tmp_path, task_file, workdir
):
    _allow_credentials(monkeypatch)
    calls = _install_fake_popen(monkeypatch, _ok_behavior)
    jobs_file = _write_jobs_file(tmp_path, [
        {"provider": "glm", "mode": "code", "task_file": task_file,
         "worktree": True},
        {"provider": "kimi-k3", "mode": "code", "task_file": task_file,
         "worktree": True},
    ])
    rc = fanout_mod.main(["--workdir", workdir, "--jobs-file", jobs_file])
    assert rc == 0
    assert all("--worktree" in cmd for cmd in calls)


def test_test_jobs_same_workdir_warn_but_proceed(
    monkeypatch, capsys, tmp_path, task_file, workdir
):
    _allow_credentials(monkeypatch)
    _install_fake_popen(monkeypatch, _ok_behavior)
    jobs_file = _write_jobs_file(tmp_path, [
        {"provider": "glm", "mode": "test", "task_file": task_file},
        {"provider": "kimi-k3", "mode": "test", "task_file": task_file},
    ])
    rc = fanout_mod.main(["--workdir", workdir, "--jobs-file", jobs_file])
    assert rc == 0
    assert "concurrent test jobs share a workdir" in capsys.readouterr().err


def test_jobs_file_unknown_field_exits_2(capsys, tmp_path, task_file, workdir):
    jobs_file = _write_jobs_file(tmp_path, [
        {"provider": "glm", "mode": "review", "task_file": task_file,
         "bogus": 1},
    ])
    rc = fanout_mod.main(["--workdir", workdir, "--jobs-file", jobs_file])
    assert rc == 2


# ---------------------------------------------------------------------------
# Aggregation / verdicts
# ---------------------------------------------------------------------------


def test_aggregation_order_and_started_reemission(
    monkeypatch, capsys, task_file, workdir
):
    _allow_credentials(monkeypatch)
    _install_fake_popen(monkeypatch, _ok_behavior)
    rc = fanout_mod.main([
        "--workdir", workdir,
        "--job", f"glm:review:{task_file}",
        "--job", f"kimi-k3:explore:{task_file}",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    results = json.loads(captured.out)
    assert len(results) == 2
    assert [r["job"] for r in results] == ["glm:review", "kimi-k3:explore"]
    assert all(r["type"] == "worker_runner.verdict" for r in results)
    started = [
        json.loads(line) for line in captured.err.splitlines()
        if line.startswith("{")
    ]
    assert {s["job"] for s in started} == {"glm:review", "kimi-k3:explore"}
    assert all(s["type"] == "worker_runner.started" for s in started)


def test_silent_child_gets_synthesized_verdict(
    monkeypatch, capsys, task_file, workdir
):
    _allow_credentials(monkeypatch)

    def behavior_for(cmd):
        if _provider_of(cmd) == "glm":
            return _ok_behavior(cmd)
        return {"stdout_lines": [], "stderr": "boom " * 200, "returncode": 2}

    _install_fake_popen(monkeypatch, behavior_for)
    rc = fanout_mod.main([
        "--workdir", workdir,
        "--job", f"glm:review:{task_file}",
        "--job", f"kimi-k3:review:{task_file}",
    ])
    assert rc == 1
    results = json.loads(capsys.readouterr().out)
    assert results[0]["job"] == "glm:review"
    assert results[0]["verdict"] == "completed"
    assert "synthesized" not in results[0]
    assert results[1]["job"] == "kimi-k3:review"
    assert results[1]["verdict"] == "error"
    assert results[1]["synthesized"] is True
    assert results[1]["exit_code"] == 2
    assert len(results[1]["stderr_tail"]) <= 500
    assert "boom" in results[1]["stderr_tail"]
    # v2 synthesized shape (D4)
    assert results[1]["schema_version"] == 2
    assert results[1]["reason"] == "crash"
    assert results[1]["parse_state"] == "unavailable"
    assert results[1]["result"] is None
    assert results[1]["final_text_path"] is None


@pytest.mark.parametrize("verdict,expected_rc", [
    ("completed", 0),
    ("step_capped_partial", 0),
    ("error", 1),
    ("empty", 1),
])
def test_exit_code_from_verdicts(
    monkeypatch, capsys, task_file, workdir, verdict, expected_rc
):
    _allow_credentials(monkeypatch)

    def behavior_for(cmd):
        provider = _provider_of(cmd)
        return {
            "stdout_lines": [
                _started_line(provider), _verdict_line(verdict, provider),
            ],
            "returncode": 0,
        }

    _install_fake_popen(monkeypatch, behavior_for)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
    ])
    assert rc == expected_rc


# ---------------------------------------------------------------------------
# v2 synthesized verdicts (D4)
# ---------------------------------------------------------------------------


def _job(provider="glm", mode="review"):
    return fanout_mod.Job(provider=provider, mode=mode, task_file="task.md")


def test_synthesized_verdict_full_v2_shape():
    verdict = fanout_mod._synthesized_verdict(_job(), 2, "tail text")
    assert verdict == {
        "job": "glm:review",
        "type": "worker_runner.verdict",
        "schema_version": 2,
        "verdict": "error",
        "synthesized": True,
        "reason": "crash",
        "exit_code": 2,
        "stderr_tail": "tail text",
        "parse_state": "unavailable",
        "result": None,
        "final_text_path": None,
    }


@pytest.mark.parametrize(
    "reason", ["crash", "timeout", "idle_timeout", "interrupted"]
)
def test_synthesized_verdict_reason_enum(reason):
    verdict = fanout_mod._synthesized_verdict(_job(), None, "", reason=reason)
    assert verdict["reason"] == reason


# ---------------------------------------------------------------------------
# Watchdog deadline computation (D4)
# ---------------------------------------------------------------------------


def test_compute_deadline_none_when_timeout_zero(monkeypatch):
    monkeypatch.setattr(runtime, "TERMINATE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(
        fanout_mod, "HARVEST_ALLOWANCE_SECONDS", 0.3, raising=False
    )
    assert fanout_mod._compute_deadline(0) is None


def test_compute_deadline_reads_constants_at_call_time(monkeypatch):
    monkeypatch.setattr(runtime, "TERMINATE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(
        fanout_mod, "HARVEST_ALLOWANCE_SECONDS", 0.3, raising=False
    )
    assert fanout_mod._compute_deadline(10) == pytest.approx(10.5)
    monkeypatch.setattr(
        fanout_mod, "HARVEST_ALLOWANCE_SECONDS", 1.0, raising=False
    )
    assert fanout_mod._compute_deadline(10) == pytest.approx(11.2)


# ---------------------------------------------------------------------------
# CLI validation (D4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1"])
def test_max_parallel_below_one_is_argparse_error(value):
    with pytest.raises(SystemExit) as excinfo:
        fanout_mod.parse_args(["--workdir", "x", "--max-parallel", value])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Spawn hardening (D4)
# ---------------------------------------------------------------------------


def test_popen_called_with_start_new_session(
    monkeypatch, capsys, task_file, workdir
):
    _allow_credentials(monkeypatch)
    recorded_kwargs = []

    def fake_popen(cmd, **kwargs):
        recorded_kwargs.append(kwargs)
        return _FakePopen(cmd, _ok_behavior(cmd))

    monkeypatch.setattr(fanout_mod.subprocess, "Popen", fake_popen)
    rc = fanout_mod.main([
        "--workdir", workdir, "--job", f"glm:review:{task_file}",
    ])
    assert rc == 0
    assert recorded_kwargs
    assert all(
        kwargs.get("start_new_session") is True for kwargs in recorded_kwargs
    )
