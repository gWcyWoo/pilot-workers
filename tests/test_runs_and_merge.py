"""The run ledger, the replicate plan, and the deterministic finding merge.

All offline: the ledger reads verdict artifacts written by earlier runs, and
the merge is pure data reduction over already-validated result blocks.
"""

from __future__ import annotations

import json
import os
import time

from pathlib import Path

import pytest

from pilot_workers import providers
from pilot_workers.cli import review_cmd, runs_cmd


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _write_verdict(provider: str, stem: str, *, mode="review",
                   verdict="completed", tokens=None, run_id=None,
                   duration=10, age_days=0.0) -> None:
    root = providers.workers_root() / "logs" / provider
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stem}.verdict.json"
    path.write_text(json.dumps({
        "run_id": run_id or stem.partition("+")[0],
        "provider": provider,
        "mode": mode,
        "verdict": verdict,
        "steps": 5,
        "duration_s": duration,
        "tokens": tokens or {"input": 1000, "output": 100, "reasoning": 50,
                             "cache_read": 0, "cache_write": 0},
    }), encoding="utf-8")
    if age_days:
        when = time.time() - age_days * 86400
        import os
        os.utime(path, (when, when))


# ----------------------------------------------------------------------
# ledger
# ----------------------------------------------------------------------


def test_no_history_is_not_an_error(isolated, capsys):
    assert runs_cmd.main([], command="runs") == 0
    assert "no runs recorded" in capsys.readouterr().out


def test_runs_lists_each_dispatch(isolated, capsys):
    _write_verdict("glm", "20260101T000000Z-aaaa")
    _write_verdict("ds", "20260101T000100Z-bbbb")
    assert runs_cmd.main([], command="runs") == 0
    out = capsys.readouterr().out
    assert "glm" in out and "ds" in out
    assert "2 runs" in out


def test_a_resumed_run_is_counted_once(isolated, capsys):
    """A resume writes a second artifact for the SAME run (`<sandbox>+<attempt>`).
    Counting both would inflate exactly the number this command exists to
    report honestly; the newest attempt wins."""
    _write_verdict("glm", "20260101T000000Z-aaaa", run_id="20260101T000000Z-aaaa")
    _write_verdict("glm", "20260101T000000Z-aaaa+retry",
                   run_id="20260101T000000Z-aaaa", tokens={"input": 9999})
    assert runs_cmd.main(["--json"], command="runs") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1


def test_filters(isolated, capsys):
    _write_verdict("glm", "a-1", mode="review")
    _write_verdict("ds", "b-1", mode="explore")
    assert runs_cmd.main(["--provider", "ds", "--json"], command="runs") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["provider"] for r in rows] == ["ds"]

    assert runs_cmd.main(["--mode", "review", "--json"], command="runs") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["mode"] for r in rows] == ["review"]


def test_since_excludes_older_runs(isolated, capsys):
    _write_verdict("glm", "old-1", age_days=30)
    _write_verdict("glm", "new-1", age_days=0)
    assert runs_cmd.main(["--since", "7d", "--json"], command="runs") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "new-1"


def test_usage_totals_tokens_per_provider(isolated, capsys):
    _write_verdict("glm", "a-1", tokens={"input": 100, "output": 10,
                                         "reasoning": 1, "cache_read": 5})
    _write_verdict("glm", "a-2", tokens={"input": 200, "output": 20,
                                         "reasoning": 2, "cache_read": 5})
    _write_verdict("ds", "b-1", tokens={"input": 7, "output": 1,
                                        "reasoning": 0, "cache_read": 0})
    assert runs_cmd.main(["--json"], command="usage") == 0
    totals = json.loads(capsys.readouterr().out)
    assert totals["glm"]["input"] == 300
    assert totals["glm"]["runs"] == 2
    assert totals["ds"]["input"] == 7


def test_a_corrupt_verdict_does_not_take_the_history_down(isolated, capsys):
    _write_verdict("glm", "good-1")
    bad = providers.workers_root() / "logs" / "glm" / "bad.verdict.json"
    bad.write_text("{not json", encoding="utf-8")
    assert runs_cmd.main(["--json"], command="runs") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1


def test_bad_flag_values_are_refused(isolated, capsys):
    assert runs_cmd.main(["--last", "many"], command="runs") == 2
    assert runs_cmd.main(["--since", "soon"], command="runs") == 2
    assert runs_cmd.main(["--bogus"], command="runs") == 2


# ----------------------------------------------------------------------
# replicate plan
# ----------------------------------------------------------------------


AXES = [{"name": "architecture"}, {"name": "security"}]


def test_default_plan_spreads_axes_across_providers():
    plan = review_cmd.build_plan(AXES, ["ds", "glm"], replicate=False)
    assert [(a["name"], p) for a, p in plan] == [
        ("architecture", "ds"), ("security", "glm")]


def test_replicate_gives_every_axis_to_every_provider():
    """The premise of cross-model review is uncorrelated blind spots, which
    only pays off when both models look at the SAME scope."""
    plan = review_cmd.build_plan(AXES, ["ds", "glm"], replicate=True)
    assert len(plan) == 4
    for axis in AXES:
        seen = {p for a, p in plan if a["name"] == axis["name"]}
        assert seen == {"ds", "glm"}


def test_one_provider_is_unchanged_by_replicate():
    single = review_cmd.build_plan(AXES, ["ds"], replicate=False)
    replicated = review_cmd.build_plan(AXES, ["ds"], replicate=True)
    assert single == replicated


# ----------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------


def _verdict_with(findings):
    return {"result": {"findings": findings}}


def test_findings_at_one_location_are_grouped_with_provenance():
    plan = review_cmd.build_plan(AXES, ["ds", "glm"], replicate=True)
    verdicts = [
        _verdict_with([{"severity": "medium", "file_line": "a.py:10",
                        "summary": "off by one"}]),
        _verdict_with([{"severity": "high", "file_line": "a.py:10",
                        "summary": "off by one"}]),
        _verdict_with([]),
        _verdict_with([]),
    ]
    merged = review_cmd.merge_findings(verdicts, plan)
    assert len(merged) == 1
    entry = merged[0]
    assert entry["file_line"] == "a.py:10"
    # Severity is the strongest anyone assigned, not the first seen.
    assert entry["severity"] == "high"
    assert len(entry["found_by"]) == 2


def test_different_defects_on_one_line_are_both_kept():
    """Grouping must never delete: two axes describing different problems at
    the same location are both real."""
    plan = review_cmd.build_plan(AXES, ["ds"], replicate=False)
    verdicts = [
        _verdict_with([{"severity": "low", "file_line": "a.py:10",
                        "summary": "unused import"}]),
        _verdict_with([{"severity": "high", "file_line": "a.py:10",
                        "summary": "sql injection"}]),
    ]
    merged = review_cmd.merge_findings(verdicts, plan)
    assert len(merged) == 1
    summaries = {f["summary"] for f in merged[0]["findings"]}
    assert summaries == {"unused import", "sql injection"}


def test_merge_sorts_by_severity_then_confirmations():
    plan = review_cmd.build_plan(AXES, ["ds"], replicate=False)
    verdicts = [
        _verdict_with([{"severity": "low", "file_line": "b.py:1", "summary": "x"}]),
        _verdict_with([{"severity": "high", "file_line": "c.py:1", "summary": "y"}]),
    ]
    merged = review_cmd.merge_findings(verdicts, plan)
    assert [e["file_line"] for e in merged] == ["c.py:1", "b.py:1"]


def test_merge_tolerates_a_verdict_with_no_result():
    """A crashed or unparsed job must not break the merge for the others."""
    plan = review_cmd.build_plan(AXES, ["ds"], replicate=False)
    verdicts = [
        {"result": None, "verdict": "error"},
        _verdict_with([{"severity": "high", "file_line": "a.py:1", "summary": "z"}]),
    ]
    merged = review_cmd.merge_findings(verdicts, plan)
    assert len(merged) == 1


def test_findings_without_a_location_still_survive():
    plan = review_cmd.build_plan(AXES, ["ds"], replicate=False)
    verdicts = [_verdict_with([{"severity": "high", "summary": "no location"}])]
    merged = review_cmd.merge_findings(verdicts, plan)
    assert merged[0]["file_line"] == "(no location)"


# ----------------------------------------------------------------------
# doctrine in the generated skill
# ----------------------------------------------------------------------


def test_the_generated_skill_says_when_not_to_dispatch(isolated, tmp_path,
                                                       monkeypatch):
    """A skill that teaches only syntax makes the host dispatch work that
    costs more than it saves — the failure the doctrine exists to prevent."""
    from pilot_workers.cli import init_cmd
    from pilot_workers.runners import get_runner

    for key in ("glm", "ds"):
        provider = providers.PROVIDERS[key]
        path = get_runner(provider.runner).credential_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {provider.provider_id: {"type": "api", "key": "k" * 20}}),
            encoding="utf-8")
        path.chmod(0o600)

    monkeypatch.chdir(tmp_path)
    assert init_cmd.main([]) == 0
    text = (tmp_path / ".claude" / "skills" / "pw9" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "When NOT to dispatch" in text
    assert "longer than the diff" in text
    assert "needs this conversation" in text
    # The budget is quoted from the CLI's own constant, not retyped.
    assert f"{review_cmd.DEFAULT_TIMEOUT_S}s" in text


# ----------------------------------------------------------------------
# discuss: independent positions, and whether they split
# ----------------------------------------------------------------------


def _position(provider, choice, position="a stance", changer="new benchmark"):
    return {"provider": provider, "result": {
        "choice": choice, "position": position,
        "reasoning": [{"point": "p", "evidence": "a.py:1"}],
        "risks": "r", "would_change_if": changer}}


def test_a_split_is_reported_as_a_split():
    from pilot_workers.cli import discuss_cmd

    summary = discuss_cmd.summarize(
        [_position("ds", "async"), _position("glm", "sync")], ["ds", "glm"])
    assert summary["split"] is True
    assert summary["distinct_choices"] == ["async", "sync"]
    assert summary["answered"] == 2


def test_agreement_is_not_reported_as_a_split():
    from pilot_workers.cli import discuss_cmd

    summary = discuss_cmd.summarize(
        [_position("ds", "async"), _position("glm", "async")], ["ds", "glm"])
    assert summary["split"] is False


def test_an_open_ended_question_needs_no_choice():
    """A question that names no options must not force a false dichotomy."""
    from pilot_workers.cli import discuss_cmd

    summary = discuss_cmd.summarize(
        [_position("ds", None), _position("glm", None)], ["ds", "glm"])
    assert summary["answered"] == 2
    assert summary["distinct_choices"] == []
    assert summary["split"] is False


def test_a_failed_job_does_not_sink_the_others():
    from pilot_workers.cli import discuss_cmd

    summary = discuss_cmd.summarize(
        [{"provider": "ds", "result": None, "verdict": "error"},
         _position("glm", "sync")], ["ds", "glm"])
    assert summary["answered"] == 1
    assert summary["positions"][0]["position"] is None


def test_the_discuss_validator_demands_a_committed_position():
    from pilot_workers.cli.dispatch import _validate_discuss_result

    good = {"position": "do it async", "choice": "async",
            "reasoning": [{"point": "p", "evidence": "a.py:1"}],
            "risks": "r", "would_change_if": "a benchmark showing X"}
    assert _validate_discuss_result(good)

    # choice may be null (open-ended question) ...
    assert _validate_discuss_result({**good, "choice": None})
    # ... but an empty position, no reasoning, or no would_change_if is not
    # a usable input to a decision.
    assert not _validate_discuss_result({**good, "position": "   "})
    assert not _validate_discuss_result({**good, "reasoning": []})
    assert not _validate_discuss_result({**good, "would_change_if": ""})


def test_discuss_runs_read_only_like_explore():
    """A discussion reads to argue; it must never edit."""
    from pilot_workers import policy

    perms = policy.agent_permissions("discuss")
    assert perms["edit"] == "deny"
    assert perms["bash"]["*>*"] == "deny"


# ----------------------------------------------------------------------
# test mode: a noise filter, not a thinker
# ----------------------------------------------------------------------


def test_the_default_is_one_suite_run(isolated):
    """`pw9 test` runs the suite. Two default layers meant two workers
    rediscovering the same command and racing on the same workdir."""
    from pilot_workers import strategies

    layers = strategies.effective("test")
    assert len(layers) == 1
    assert layers[0]["name"] == "suite"


def test_known_failures_reach_the_generated_task(isolated):
    """Without a baseline every pre-existing failure comes back as a new
    finding — the opposite of the context protection this mode is for."""
    from pilot_workers import strategies
    from pilot_workers.cli import test_cmd

    layer = strategies.effective("test")[0]
    path = test_cmd._generate_task(layer, "/tmp", "tests/x.py::y — broken since May")
    try:
        text = Path(path).read_text(encoding="utf-8")
    finally:
        os.unlink(path)
    assert "Known Pre-existing Failures" in text
    assert "broken since May" in text
    assert "NOT a new finding" in text


def test_the_task_says_run_and_report_not_diagnose(isolated):
    from pilot_workers import strategies
    from pilot_workers.cli import test_cmd

    layer = strategies.effective("test")[0]
    path = test_cmd._generate_task(layer, "/tmp")
    try:
        text = Path(path).read_text(encoding="utf-8")
    finally:
        os.unlink(path)
    assert "none" in text            # empty baseline renders as "none"
    assert "Do not diagnose" in text


def test_the_test_result_schema_caps_what_comes_back(isolated):
    """The value is that the suite's output dies in the worker: counts plus
    capped per-failure text, never the whole log."""
    import pilot_workers
    from pilot_workers import policy

    prompt = (Path(pilot_workers.__file__).resolve().parent
              / "prompts" / "test.md").read_text(encoding="utf-8")
    assert "capped at 40 lines" in prompt
    assert '"passed"' in prompt and '"failed"' in prompt
    # And the mode may run tests but never edit source.
    perms = policy.agent_permissions("test")
    assert perms["edit"] == "deny"
