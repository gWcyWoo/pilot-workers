"""This tool's own child processes inherit a whitelist, not the shell.

``fanout`` spawns ``dispatch``, which spawns ``run``. None of them needs the
API keys a developer exports in their shell, and an environment they do not
need is an environment that leaks: a core dump or ``/proc/<pid>/environ``
exposes every variable the process holds. The worker's own env is rebuilt from
SAFE_ENV_KEYS regardless, so filtering costs nothing.
"""

from __future__ import annotations

import subprocess

from pilot_workers import runtime
from pilot_workers.cli import dispatch as dispatch_mod


def test_an_unrelated_exported_key_does_not_reach_the_child(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key-just-a-fixture")
    monkeypatch.setenv("HF_TOKEN", "also-a-fixture")
    env = dispatch_mod._child_environment()
    assert "OPENAI_API_KEY" not in env
    assert "HF_TOKEN" not in env


def test_the_child_still_gets_what_it_needs(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    env = dispatch_mod._child_environment()
    # Path resolution, the interpreter, and import pinning all survive.
    assert env["PILOT_WORKERS_HOME"] == str(tmp_path)
    assert env["HOME"]
    assert env["PATH"]
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONPATH"]


def test_a_real_child_resolves_the_same_pilot_home(monkeypatch, tmp_path):
    """Filtering must not break the child's view of where things live."""
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    cmd = dispatch_mod._build_runner_command(
        "glm", "review", "/tmp", None, "/tmp/t.md", None, False, 60, 60)
    proc = subprocess.run(
        [cmd[0], "-c",
         "from pilot_workers import providers; print(providers.pilot_home())"],
        capture_output=True, text=True, check=True,
        env=dispatch_mod._child_environment(), cwd=dispatch_mod._child_cwd(),
    )
    assert proc.stdout.strip() == str(tmp_path)


def test_fanout_passes_the_filtered_env_to_its_dispatch_child(
        monkeypatch, tmp_path):
    """fanout's Popen had no env= at all, so it handed the child everything."""
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key-just-a-fixture")
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    captured: dict = {}

    class Recorder:
        def __init__(self, cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            raise OSError("spawn refused by the test")

    monkeypatch.setattr("pilot_workers.cli.fanout.subprocess.Popen", Recorder)
    monkeypatch.setattr(
        runtime, "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": True})
    from pilot_workers.cli import fanout as fanout_mod

    fanout_mod.main([
        "--workdir", str(tmp_path), "--job", f"glm:review:{task}"])

    env = captured["env"]
    assert env is not None, "fanout spawned its child with an inherited env"
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHONSAFEPATH"] == "1"


def test_the_orchestrator_whitelist_is_the_worker_whitelist_plus_paths():
    """One list, one place: divergence is how a var quietly comes back."""
    extra = set(runtime.ORCHESTRATOR_ENV_KEYS) - set(runtime.SAFE_ENV_KEYS)
    assert extra == {"PILOT_WORKERS_HOME", "CODEX_HOME"}
