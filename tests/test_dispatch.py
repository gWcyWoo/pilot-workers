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


def test_build_verdict_invalid_block_is_unstructured():
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
    assert verdict["parse_state"] == "unstructured"
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
        "glm", "resume", "/tmp", "task text", None, "s-1", False, 60, 30,
        run_id="r-1",
    )
    assert "--run-id" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "r-1"
    assert cmd[cmd.index("--session") + 1] == "s-1"


def test_build_runner_command_cold_mode_omits_run_id():
    cmd = dispatch_mod._build_runner_command(
        "glm", "code", "/tmp", "task text", None, None, False, 60, 30,
    )
    assert "--run-id" not in cmd
