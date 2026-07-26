"""Offline tests for pilot_workers.cli.dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from pilot_workers.cli import dispatch as dispatch_mod
from pilot_workers.runners import get_runner


def _write_jsonl(path, events):
    lines = []
    for event in events:
        if isinstance(event, str):
            lines.append(event)
        else:
            lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _step_finish(tokens):
    return {"type": "step_finish", "part": {"tokens": tokens}}


def _sample_parsed(final_text="", steps=0, has_error_event=False):
    return {
        "steps": steps,
        "tokens": {
            "input": 0, "output": 0, "reasoning": 0,
            "cache_read": 0, "cache_write": 0,
        },
        "tool_errors": {"permission_denied": 0, "other": 0},
        "final_text": final_text,
        "has_error_event": has_error_event,
        "duration_s": None,
    }


LONG_TEXT = "x" * 250

EXPLORE_RESULT = {
    "facts": [{"fact": "dispatch parses jsonl", "file_line": "src/x.py:1"}],
    "truncated": False,
    "more_in": ["src/"],
}


def _explore_text_with_block():
    return (
        "Report prose.\n"
        "<!--PILOT_RESULT_BEGIN-->\n"
        + json.dumps(EXPLORE_RESULT)
        + "\n<!--PILOT_RESULT_END-->\n"
    )


def test_parse_jsonl_counts_steps_tokens_and_tool_errors(tmp_path):
    events = [
        {"type": "step_finish", "timestamp": 1000,
         "part": {"tokens": {"input": 10, "output": 5, "reasoning": 2,
                             "cache": {"read": 3, "write": 1}}}},
        {"type": "step_finish", "timestamp": 2000,
         "part": {"tokens": {"input": 20, "output": 7, "reasoning": 0,
                             "cache": {"read": 0, "write": 0}}}},
        {"type": "step_finish", "timestamp": 4000,
         "part": {"tokens": {"input": 1, "output": 1, "reasoning": 1,
                             "cache": {"read": 1, "write": 1}}}},
        {"type": "text", "part": {"text": "first answer"}},
        {"type": "text", "part": {"text": "final answer"}},
        {"type": "tool_use", "part": {"state": {
            "status": "error",
            "error": "The user has specified a rule which prevents this",
        }}},
        {"type": "tool_use", "part": {"state": {
            "status": "error", "error": "some other failure",
        }}},
    ]
    path = _write_jsonl(tmp_path / "run.jsonl", events)
    parsed = dispatch_mod.parse_jsonl(path, get_runner("opencode"))

    assert parsed["steps"] == 3
    assert parsed["tokens"]["input"] == 31
    assert parsed["tokens"]["output"] == 13
    assert parsed["tokens"]["reasoning"] == 3
    assert parsed["tokens"]["cache_read"] == 4
    assert parsed["tokens"]["cache_write"] == 2
    assert parsed["tool_errors"]["permission_denied"] == 1
    assert parsed["tool_errors"]["other"] == 1
    assert parsed["final_text"] == "final answer"
    assert parsed["has_error_event"] is False
    assert parsed["duration_s"] == 3


def test_parse_jsonl_skips_bad_lines(tmp_path):
    path = _write_jsonl(tmp_path / "bad.jsonl", [
        "not json at all",
        json.dumps(["a", "list", "not", "dict"]),
        json.dumps("a plain string"),
        "",
        {"type": "text", "part": {"text": "hello"}},
    ])
    parsed = dispatch_mod.parse_jsonl(path, get_runner("opencode"))
    assert parsed["steps"] == 0
    assert parsed["final_text"] == "hello"


# ---------------------------------------------------------------------------
# classify_verdict (v2: 4 positional args, design-doc frozen order)
# ---------------------------------------------------------------------------


def test_classify_verdict_summary_nonzero_exit_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 1}, "unavailable") == "error"


def test_classify_verdict_summary_timed_out_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0, "timed_out": True}, "unavailable") == "error"


def test_classify_verdict_summary_idle_timed_out_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0, "idle_timed_out": True},
        "unstructured") == "error"


def test_classify_verdict_summary_interrupted_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0, "interrupted": True},
        "unavailable") == "error"


def test_classify_verdict_steps_at_cap_is_step_capped_partial():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=10)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unavailable") == "step_capped_partial"


def test_classify_verdict_step_capped_beats_parsed():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=10)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "parsed") == "step_capped_partial"


def test_classify_verdict_step_capped_beats_summary_error():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=10)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 1}, "unstructured") == "step_capped_partial"


def test_classify_verdict_parsed_beats_nonzero_exit():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 1}, "parsed") == "completed"


def test_classify_verdict_parsed_beats_timed_out():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0, "timed_out": True}, "parsed") == "completed"


def test_classify_verdict_parsed_beats_short_text():
    parsed = _sample_parsed(final_text="short")
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "parsed") == "completed"


def test_classify_verdict_short_final_text_is_empty():
    parsed = _sample_parsed(final_text="short")
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unavailable") == "empty"


def test_classify_verdict_long_text_ok_summary_is_completed():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=3)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unstructured") == "completed"


def test_classify_verdict_unstructured_long_text_is_completed():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unstructured") == "completed"


def test_classify_verdict_no_summary_error_event_short_text_is_error():
    parsed = _sample_parsed(final_text="short", has_error_event=True)
    assert dispatch_mod.classify_verdict(parsed, 10, None, "unavailable") == "error"


def test_classify_verdict_no_summary_error_event_long_unstructured_is_completed():
    # v0.4.0 parity: the length guard lets long unstructured text with a
    # transient error event fall through to rule 4.
    parsed = _sample_parsed(final_text=LONG_TEXT, has_error_event=True)
    assert dispatch_mod.classify_verdict(
        parsed, 10, None, "unstructured") == "completed"


def test_classify_verdict_unavailable_short_text_no_errors_is_empty():
    parsed = _sample_parsed(final_text="short")
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unavailable") == "empty"


# ---------------------------------------------------------------------------
# build_verdict (v2 schema)
# ---------------------------------------------------------------------------


def _verdict_for(final_text, summary, stderr_text=""):
    return dispatch_mod.build_verdict(
        run_id="run-e", provider="glm", runner="opencode", mode="explore",
        parsed=_sample_parsed(final_text=final_text), summary=summary,
        jsonl_path="/tmp/run-e.jsonl", stderr_path="/tmp/run-e.stderr.log",
        step_cap=10, report_path="/tmp/run-e.report.md",
        child_stderr_text=stderr_text,
    )


def test_an_error_verdict_carries_the_child_stderr_tail():
    """The two verdicts that most need diagnosing carried the least.

    fanout's synthesized verdicts already embedded a stderr tail; a real error
    verdict named two file paths instead, so the planner had to open them —
    the round trip the two-line contract exists to avoid.
    """
    verdict = _verdict_for("short", {"exit_code": 1},
                           stderr_text="Traceback: boom at line 3\n")
    assert verdict["verdict"] == "error"
    assert "boom at line 3" in verdict["stderr_tail"]


def test_an_empty_verdict_carries_the_child_stderr_tail():
    verdict = _verdict_for("", {"exit_code": 0},
                           stderr_text="model returned nothing\n")
    assert verdict["verdict"] == "empty"
    assert "model returned nothing" in verdict["stderr_tail"]


def test_the_stderr_tail_is_bounded():
    verdict = _verdict_for("short", {"exit_code": 1}, stderr_text="x" * 5000)
    assert len(verdict["stderr_tail"]) == dispatch_mod.STDERR_TAIL_BYTES


def test_a_completed_verdict_does_not_carry_a_stderr_tail():
    """Heartbeat chatter on a healthy run is not worth a byte of context."""
    verdict = _verdict_for(_explore_text_with_block(), {"exit_code": 0},
                           stderr_text="heartbeat ...\n")
    assert verdict["verdict"] == "completed"
    assert verdict["stderr_tail"] == ""


def test_build_verdict_shape():
    final_text = _explore_text_with_block()
    parsed = _sample_parsed(final_text=final_text, steps=2)
    verdict = dispatch_mod.build_verdict(
        run_id="run-1",
        provider="glm",
        runner="opencode",
        mode="explore",
        parsed=parsed,
        summary={"exit_code": 0},
        jsonl_path="/tmp/run-1.jsonl",
        stderr_path=None,
        step_cap=10,
        report_path="/tmp/run-1.report.md",
    )
    assert verdict["type"] == "worker_runner.verdict"
    assert verdict["schema_version"] == 2
    assert verdict["final_text_len"] == len(final_text)
    assert verdict["verdict"] == "completed"
    assert verdict["runner"] == "opencode"
    assert verdict["parse_state"] == "parsed"
    assert verdict["result"] == EXPLORE_RESULT
    assert verdict["final_text_path"] == "/tmp/run-1.report.md"
    assert "final_text" not in verdict


def test_build_verdict_without_block_is_unavailable():
    parsed = _sample_parsed(final_text="plain report without block", steps=1)
    verdict = dispatch_mod.build_verdict(
        run_id="run-2",
        provider="glm",
        runner="opencode",
        mode="explore",
        parsed=parsed,
        summary={"exit_code": 0},
        jsonl_path="/tmp/run-2.jsonl",
        stderr_path=None,
        step_cap=10,
        report_path=None,
    )
    assert verdict["schema_version"] == 2
    assert verdict["parse_state"] == "unavailable"
    assert verdict["result"] is None
    assert verdict["final_text_path"] is None
    assert "final_text" not in verdict
    assert verdict["final_text_len"] == len("plain report without block")


def test_build_verdict_invalid_block_is_malformed():
    final_text = (
        "prose\n<!--PILOT_RESULT_BEGIN-->\n{not json\n"
        "<!--PILOT_RESULT_END-->\n" + "y" * 200
    )
    parsed = _sample_parsed(final_text=final_text, steps=1)
    verdict = dispatch_mod.build_verdict(
        run_id="run-3",
        provider=None,
        runner="opencode",
        mode="explore",
        parsed=parsed,
        summary=None,
        jsonl_path="/tmp/run-3.jsonl",
        stderr_path=None,
        step_cap=10,
        report_path="/tmp/run-3.report.md",
    )
    # `malformed`, not `unstructured`: the block was there and finished, so the
    # planner should open final_text_path rather than assume nothing was written.
    assert verdict["parse_state"] == "malformed"
    assert verdict["result"] is None
    assert "final_text" not in verdict


# ---------------------------------------------------------------------------
# Artifact writers
# ---------------------------------------------------------------------------


def test_write_verdict_file_mode_0600(tmp_path):
    path = tmp_path / "verdict.json"
    dispatch_mod.write_verdict_file(path, {"type": "worker_runner.verdict"})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "type": "worker_runner.verdict"}


def test_write_verdict_file_compact_single_line_no_tmp_leftovers(tmp_path):
    path = tmp_path / "verdict.json"
    verdict = {
        "type": "worker_runner.verdict",
        "schema_version": 2,
        "nested": {"a": [1, 2, 3]},
    }
    dispatch_mod.write_verdict_file(path, verdict)
    raw = path.read_text(encoding="utf-8")
    body = raw[:-1] if raw.endswith("\n") else raw
    assert "\n" not in body
    assert json.loads(raw) == verdict
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob("*.tmp*"))


def test_write_report_file_writes_exact_text_and_mode_0600(tmp_path):
    path = tmp_path / "run.report.md"
    text = "line one\nline two <!--PILOT_RESULT_BEGIN--> stays verbatim\n"
    dispatch_mod.write_report_file(path, text)
    assert path.read_text(encoding="utf-8") == text
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# main / reparse
# ---------------------------------------------------------------------------


def test_main_reparse_with_dispatch_args_returns_2(capsys):
    rc = dispatch_mod.main(["--reparse", "x.jsonl", "--provider", "glm"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_main_reparse_without_mode_returns_2(capsys):
    rc = dispatch_mod.main(["--reparse", "x.jsonl"])
    assert rc == 2
    assert "--mode is required" in capsys.readouterr().err


def test_main_reparse_full_pipeline(tmp_path, capsys):
    events = [
        _step_finish({"input": 5, "output": 5, "reasoning": 0,
                      "cache": {"read": 0, "write": 0}}),
        {"type": "text", "part": {"text": LONG_TEXT}},
    ]
    path = _write_jsonl(tmp_path / "run.jsonl", events)

    rc = dispatch_mod.main(["--reparse", str(path), "--mode", "explore"])
    assert rc == 0
    out = capsys.readouterr().out
    verdict = json.loads(out.strip().splitlines()[-1])
    assert verdict["type"] == "worker_runner.verdict"
    assert verdict["verdict"] == "completed"
    assert verdict["schema_version"] == 2
    assert verdict["parse_state"] == "unavailable"
    assert verdict["result"] is None
    assert "final_text" not in verdict
    report_path = tmp_path / "run.report.md"
    assert report_path.is_file()
    assert Path(verdict["final_text_path"]).resolve() == report_path.resolve()
    assert report_path.read_text(encoding="utf-8") == LONG_TEXT


def test_main_reparse_report_md_idempotent(tmp_path, capsys):
    events = [{"type": "text", "part": {"text": LONG_TEXT}}]
    path = _write_jsonl(tmp_path / "run.jsonl", events)

    rc = dispatch_mod.main(["--reparse", str(path), "--mode", "explore"])
    assert rc == 0
    capsys.readouterr()
    report_path = tmp_path / "run.report.md"
    first = report_path.read_text(encoding="utf-8")

    rc = dispatch_mod.main(["--reparse", str(path), "--mode", "explore"])
    assert rc == 0
    capsys.readouterr()
    assert report_path.read_text(encoding="utf-8") == first


def test_main_reparse_no_text_events_writes_sentinel(tmp_path, capsys):
    events = [
        _step_finish({"input": 1, "output": 1, "reasoning": 0,
                      "cache": {"read": 0, "write": 0}}),
    ]
    path = _write_jsonl(tmp_path / "run.jsonl", events)

    rc = dispatch_mod.main(["--reparse", str(path), "--mode", "explore"])
    assert rc == 0
    capsys.readouterr()
    report_path = tmp_path / "run.report.md"
    assert report_path.is_file()
    assert report_path.read_text(encoding="utf-8").splitlines() == [
        "no model output"]


def test_main_reparse_with_result_block(tmp_path, capsys):
    final_text = _explore_text_with_block()
    events = [{"type": "text", "part": {"text": final_text}}]
    path = _write_jsonl(tmp_path / "run.jsonl", events)

    rc = dispatch_mod.main(["--reparse", str(path), "--mode", "explore"])
    assert rc == 0
    out = capsys.readouterr().out
    verdict = json.loads(out.strip().splitlines()[-1])
    assert verdict["schema_version"] == 2
    assert verdict["parse_state"] == "parsed"
    assert verdict["result"] == EXPLORE_RESULT
    assert verdict["verdict"] == "completed"
    assert "final_text" not in verdict
    assert verdict["final_text_len"] == len(final_text)
    report_path = tmp_path / "run.report.md"
    assert report_path.read_text(encoding="utf-8") == final_text


# ---------------------------------------------------------------------------
# Boundary tests (parse_state="unavailable")
# ---------------------------------------------------------------------------


def test_classify_verdict_text_exactly_200_is_completed():
    text_200 = "x" * 200
    parsed = _sample_parsed(final_text=text_200, steps=3)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unavailable") == "completed"


def test_classify_verdict_text_199_is_empty():
    text_199 = "x" * 199
    parsed = _sample_parsed(final_text=text_199, steps=3)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}, "unavailable") == "empty"


# ---------------------------------------------------------------------------
# D5: --run-id resume plumbing (resume keyed by --session + --run-id)
# ---------------------------------------------------------------------------


def test_parse_args_accepts_run_id():
    args = dispatch_mod.parse_args([
        "--mode", "resume", "--provider", "glm", "--workdir", "/tmp",
        "--task", "t", "--session", "s-1", "--run-id", "r-1",
    ])
    assert args.run_id == "r-1"


def test_main_resume_without_run_id_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    rc = dispatch_mod.main([
        "--mode", "resume", "--provider", "glm", "--workdir", "/tmp",
        "--task", "t", "--session", "s-1",
    ])
    assert rc == 2
    assert "--run-id" in capsys.readouterr().err


def test_build_runner_command_resume_includes_run_id():
    cmd = dispatch_mod._build_runner_command(
        "glm", "resume", "/tmp", None, "/tmp/t.md", "s-1", False, 60, 30,
        run_id="r-1",
    )
    assert "--run-id" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "r-1"
    assert cmd[cmd.index("--session") + 1] == "s-1"


def test_build_runner_command_cold_mode_omits_run_id():
    cmd = dispatch_mod._build_runner_command(
        "glm", "code", "/tmp", None, "/tmp/t.md", None, False, 60, 30,
    )
    assert "--run-id" not in cmd


def test_the_stderr_tail_redacts_every_configured_key(monkeypatch):
    """run_process redacts only the key it was handed.

    A worker's stderr can mention a DIFFERENT configured provider's key; that
    was contained while the text stayed in a local 0600 log, but the tail goes
    into the verdict, which goes to the planner.
    """
    from pilot_workers import runtime

    other_key = "fake-key-for-another-provider-000111"
    monkeypatch.setattr(runtime, "configured_secrets", lambda: [other_key])

    verdict = _verdict_for(
        "short", {"exit_code": 1},
        stderr_text=f"provider rejected {other_key} while probing\n")

    assert other_key not in verdict["stderr_tail"]
    assert "[REDACTED]" in verdict["stderr_tail"]
    assert "while probing" in verdict["stderr_tail"], "redaction ate the message"


def test_redaction_ignores_short_values(monkeypatch):
    """A 3-char 'secret' occurs in ordinary output; replacing it would corrupt
    the very text the tail exists to convey."""
    from pilot_workers import runtime

    assert runtime.redact_secrets("the cat sat", ["cat"]) == "the cat sat"


# ---------------------------------------------------------------------------
# A wrapper that died after `started` reports no summary — its exit code is the
# only evidence left, and discarding it turned a kill into "completed".
# ---------------------------------------------------------------------------


def test_no_summary_with_a_nonzero_child_exit_is_an_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, None, "unavailable", 1) == "error"


def test_no_summary_with_a_signal_death_is_an_error():
    """POSIX reports a signal kill as a negative return code (SIGKILL -> -9)."""
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, None, "unavailable", -9) == "error"


def test_no_summary_with_a_clean_child_exit_keeps_the_old_reading():
    """A zero exit with long text really is a completion; only failure changes."""
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, None, "unavailable", 0) == "completed"


def test_a_parsed_result_still_wins_over_a_nonzero_child_exit():
    """Rule order is unchanged: a valid result block is a completion."""
    parsed = _sample_parsed(final_text=_explore_text_with_block())
    assert dispatch_mod.classify_verdict(parsed, 10, None, "parsed", 1) == "completed"


def test_the_verdict_reports_the_child_exit_code_when_there_is_no_summary():
    """`exit_code: null` told the planner "it failed and we do not know why"
    while the number was in hand."""
    verdict = dispatch_mod.build_verdict(
        run_id="run-k", provider="glm", runner="opencode", mode="explore",
        parsed=_sample_parsed(final_text=LONG_TEXT), summary=None,
        jsonl_path="/tmp/run-k.jsonl", stderr_path=None, step_cap=10,
        report_path="/tmp/run-k.report.md", child_exit_code=-9,
    )
    assert verdict["exit_code"] == -9
    assert verdict["verdict"] == "error"


def test_redaction_at_exactly_the_minimum_length():
    """Boundary for `>= MIN_REDACTABLE_SECRET`: a 12-char secret IS redacted."""
    from pilot_workers import runtime

    secret = "a" * runtime.MIN_REDACTABLE_SECRET
    assert secret not in runtime.redact_secrets(f"saw {secret} here", [secret])
    shorter = "a" * (runtime.MIN_REDACTABLE_SECRET - 1)
    assert shorter in runtime.redact_secrets(f"saw {shorter} here", [shorter])


def test_no_summary_error_event_at_the_empty_text_threshold():
    """The `summary is None` + error-event branch was only tested at 5 and 250
    characters, so the threshold comparison could flip unnoticed."""
    at = _sample_parsed(
        final_text="x" * dispatch_mod.EMPTY_FINAL_TEXT_THRESHOLD, has_error_event=True)
    below = _sample_parsed(
        final_text="x" * (dispatch_mod.EMPTY_FINAL_TEXT_THRESHOLD - 1),
        has_error_event=True)
    assert dispatch_mod.classify_verdict(at, 999, None, "unavailable") == "completed"
    assert dispatch_mod.classify_verdict(below, 999, None, "unavailable") == "error"


def test_run_id_outside_resume_is_refused_not_dropped(tmp_path, monkeypatch, capsys):
    """`_build_runner_command` forwards --run-id only for resume, and run.py
    rejects it for every other mode, so a planner passing it to a cold dispatch
    got neither the sandbox it named nor a word about it. Pre-existing: the line
    is at HEAD. Same class as the swallowed --global-key."""
    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    rc = dispatch_mod.main([
        "--provider", "glm", "--mode", "review", "--workdir", str(tmp_path),
        "--task-file", str(task), "--run-id", "20260101T000000Z-abcdef01"])
    assert rc != 0
    assert "--run-id is only valid with --mode resume" in capsys.readouterr().err
