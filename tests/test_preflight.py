"""Cheap complete checks that belong before the expensive work.

A guard that runs after N children have each paid full interpreter + YAML +
credential startup has already wasted the thing it exists to save.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pilot_workers import runtime
from pilot_workers.cli import dispatch as dispatch_mod
from pilot_workers.cli import fanout as fanout_mod


def test_fanout_rejects_a_missing_workdir_before_spawning(tmp_path, capsys):
    """`--workdir` was only checked for emptiness. A path that does not exist let
    every child spawn and fail identically, N times over."""
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    missing = tmp_path / "nope"

    rc = fanout_mod.main([
        "--workdir", str(missing),
        "--job", f"glm:review:{task}",
        "--job", f"ds:review:{task}",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert "nope" in err or "work directory" in err.lower()
    assert "worker_runner" not in err, "children were spawned before the check"


def test_fanout_rejects_a_workdir_that_is_a_file(tmp_path, capsys):
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    rc = fanout_mod.main([
        "--workdir", str(not_a_dir), "--job", f"glm:review:{task}",
    ])
    assert rc != 0
    assert "director" in capsys.readouterr().err.lower()


# ----------------------------------------------------------------------
# result validation must reject counts that contradict the findings
# ----------------------------------------------------------------------


def _review_payload(findings, **counts):
    base = {"high": 0, "medium": 0, "low": 0}
    base.update(counts)
    return {"overall": "summary", "severity_counts": base, "findings": findings}


def _finding(severity):
    return {"severity": severity, "file_line": "a.py:1", "summary": "s",
            "impact": "i", "suggested_fix": "f"}


def test_counts_matching_the_findings_validate():
    payload = _review_payload([_finding("high"), _finding("low")], high=1, low=1)
    assert dispatch_mod._validate_review_result(payload)


def test_counts_contradicting_the_findings_are_rejected():
    """A worker reporting 5 high findings while listing none is not a valid
    result — trusting the counts would misreport severity to the planner."""
    payload = _review_payload([_finding("low")], high=5, low=1)
    assert not dispatch_mod._validate_review_result(payload)


def test_an_unknown_severity_is_rejected():
    payload = _review_payload([_finding("catastrophic")], high=0, low=0)
    assert not dispatch_mod._validate_review_result(payload)


def test_negative_counts_are_rejected():
    payload = _review_payload([], high=-1)
    assert not dispatch_mod._validate_review_result(payload)


def test_an_empty_file_line_is_rejected():
    """A finding with no location cannot be checked, so it is not a finding."""
    bad = _finding("low")
    bad["file_line"] = ""
    assert not dispatch_mod._validate_review_result(
        _review_payload([bad], low=1))


def test_negative_test_counts_are_rejected():
    """A test result claiming -1 failures is malformed; accepting it let the
    verdict read `completed` on nonsense."""
    payload = {"command": "pytest -q", "passed": -1, "failed": 0, "failures": []}
    assert not dispatch_mod._validate_test_result(payload)


def test_valid_test_counts_still_validate():
    payload = {"command": "pytest -q", "passed": 3, "failed": 0, "failures": []}
    assert dispatch_mod._validate_test_result(payload)


def test_extra_severity_keys_are_rejected():
    """Only high/medium/low were inspected, so `critical: 99` rode along
    unvalidated and unreported."""
    payload = _review_payload([], high=0)
    payload["severity_counts"]["critical"] = 99
    assert not dispatch_mod._validate_review_result(payload)


def test_more_in_is_optional_when_nothing_was_truncated():
    """prompts/explore.md labels more_in 'if truncated', but the validator made
    it mandatory — so a complete exploration was scored unstructured."""
    payload = {"facts": [{"fact": "f", "file_line": "a.py:1"}],
               "truncated": False}
    assert dispatch_mod._validate_explore_result(payload)


def test_more_in_is_required_when_output_was_truncated():
    payload = {"facts": [{"fact": "f", "file_line": "a.py:1"}], "truncated": True}
    assert not dispatch_mod._validate_explore_result(payload)


def test_fanout_hands_the_child_absolute_paths(tmp_path, monkeypatch):
    """The child runs in a cwd this tool owns, so relative paths must be
    resolved by the PARENT — in the caller's cwd, where the user meant them.

    Before the child got its own cwd this was harmless: it inherited ours.
    Afterwards, a relative --workdir or task file passed the parent's existence
    checks and then resolved to a different place (or nowhere) in the child.
    """
    project = tmp_path / "proj"
    project.mkdir()
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cmd = fanout_mod._build_dispatch_command(
        fanout_mod.Job(provider="glm", mode="review", task_file="t.md"),
        "proj", 60, 60)

    workdir = cmd[cmd.index("--workdir") + 1]
    task_arg = cmd[cmd.index("--task-file") + 1]
    assert Path(workdir).is_absolute() and Path(task_arg).is_absolute()
    assert Path(workdir).resolve() == project.resolve()
    assert Path(task_arg).resolve() == task.resolve()


def test_fanout_accepts_a_relative_task_file_end_to_end(tmp_path, monkeypatch, capsys):
    """The preflight and the spawned command must agree about what a relative
    path means — the preflight said the file existed, so the child must find it."""
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        runtime, "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": True})

    captured: dict = {}

    class Recorder:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            raise OSError("spawn refused by the test")

    monkeypatch.setattr("pilot_workers.cli.fanout.subprocess.Popen", Recorder)
    fanout_mod.main(["--workdir", ".", "--job", "glm:review:t.md"])

    task_arg = captured["cmd"][captured["cmd"].index("--task-file") + 1]
    assert Path(task_arg).is_file(), (
        "the child was handed a path that does not resolve from its own cwd")


# ----------------------------------------------------------------------
# The task-content guard belongs in the preflight too.
#
# `taskguard` runs inside each `run` child, so every job is guarded and no
# secret is ever sent. But a fanout of N jobs whose LAST task file carries an
# unfilled placeholder — or a credential — dispatched the first N-1 to paid
# third-party endpoints before saying so. That is precisely what this module's
# docstring says a guard must not do.
# ----------------------------------------------------------------------

def _no_spawn(monkeypatch) -> dict:
    """Make any dispatch spawn observable and harmless."""
    monkeypatch.setattr(
        runtime, "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": True})
    spawned: dict = {"count": 0}

    class Recorder:
        def __init__(self, cmd, **kwargs):
            spawned["count"] += 1
            raise OSError("spawn refused by the test")

    monkeypatch.setattr("pilot_workers.cli.fanout.subprocess.Popen", Recorder)
    return spawned


@pytest.mark.parametrize("poison,expected", [
    ("<!--PILOT_FILL: describe the change-->\n", "placeholder"),
    ("AWS_SESSION_TOKEN=FwoGZXIvYXdzEBYaDHexample1234567890\n", "credential"),
    ("", "empty"),
])
def test_fanout_refuses_a_bad_task_before_spawning_anything(
        tmp_path, monkeypatch, capsys, poison, expected):
    good = tmp_path / "good.md"
    good.write_text("review the parser", encoding="utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text(poison, encoding="utf-8")
    spawned = _no_spawn(monkeypatch)

    rc = fanout_mod.main([
        "--workdir", str(tmp_path),
        "--job", f"glm:review:{good}",
        "--job", f"ds:review:{bad}",
    ])
    err = capsys.readouterr().err
    assert rc != 0, "a poisoned task file was accepted"
    assert spawned["count"] == 0, (
        f"{spawned['count']} job(s) were dispatched before the refusal")
    assert str(bad) in err, f"the error does not name the offending file: {err}"
    assert expected in err.lower(), err


def test_the_preflight_does_not_refuse_an_ordinary_fanout(tmp_path, monkeypatch):
    """Reverse assertion: the new check must not block real task files, and a
    task file shared by several jobs must be read without complaint."""
    task = tmp_path / "t.md"
    task.write_text("review the parser for off-by-one errors", encoding="utf-8")
    spawned = _no_spawn(monkeypatch)

    fanout_mod.main([
        "--workdir", str(tmp_path),
        "--providers", "glm,ds", "--mode", "review", "--task-file", str(task),
    ])
    assert spawned["count"] == 2, (
        f"the preflight blocked an ordinary fanout ({spawned['count']} spawns)")


def test_fanout_rejects_an_insecure_credential_before_spawning(tmp_path, monkeypatch, capsys):
    """credential_key refuses a file wider than 0600 on the dispatch path, so
    without this check every child spawned and failed on the same thing."""
    task = tmp_path / "t.md"
    task.write_text("review the parser", encoding="utf-8")
    spawned = {"count": 0}

    monkeypatch.setattr(
        runtime, "credential_metadata",
        lambda provider, runner: {"configured": True, "secure_mode": False,
                                  "path": "/somewhere/auth.json"})

    class Recorder:
        def __init__(self, cmd, **kwargs):
            spawned["count"] += 1
            raise OSError("spawn refused by the test")

    monkeypatch.setattr("pilot_workers.cli.fanout.subprocess.Popen", Recorder)
    rc = fanout_mod.main(["--workdir", str(tmp_path), "--job", f"glm:review:{task}"])
    assert rc != 0
    assert spawned["count"] == 0, "a job was dispatched despite an insecure key"
    assert "not private" in capsys.readouterr().err
