"""Offline tests for pilot_workers.cli.dispatch."""

from __future__ import annotations

import json

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


def test_classify_verdict_summary_nonzero_exit_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(parsed, 10, {"exit_code": 1}) == "error"


def test_classify_verdict_summary_timed_out_is_error():
    parsed = _sample_parsed(final_text=LONG_TEXT)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0, "timed_out": True}) == "error"


def test_classify_verdict_steps_at_cap_is_step_capped_partial():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=10)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}) == "step_capped_partial"


def test_classify_verdict_short_final_text_is_empty():
    parsed = _sample_parsed(final_text="short")
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}) == "empty"


def test_classify_verdict_long_text_ok_summary_is_completed():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=3)
    assert dispatch_mod.classify_verdict(
        parsed, 10, {"exit_code": 0}) == "completed"


def test_classify_verdict_no_summary_error_event_short_text_is_error():
    parsed = _sample_parsed(final_text="short", has_error_event=True)
    assert dispatch_mod.classify_verdict(parsed, 10, None) == "error"


def test_build_verdict_shape():
    parsed = _sample_parsed(final_text=LONG_TEXT, steps=2)
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
    )
    assert verdict["type"] == "worker_runner.verdict"
    assert verdict["schema_version"] == 1
    assert verdict["final_text_len"] == len(LONG_TEXT)
    assert verdict["verdict"] == "completed"
    assert verdict["runner"] == "opencode"


def test_write_verdict_file_mode_0600(tmp_path):
    path = tmp_path / "verdict.json"
    dispatch_mod.write_verdict_file(path, {"type": "worker_runner.verdict"})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "type": "worker_runner.verdict"}


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
