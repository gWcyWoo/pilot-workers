"""Offline tests for extract_result (D3 structured verdict, schema v2)."""

from __future__ import annotations

import json

import pytest

from pilot_workers.cli import dispatch as dispatch_mod

RESULT_BEGIN = "<!--PILOT_RESULT_BEGIN-->"
RESULT_END = "<!--PILOT_RESULT_END-->"

EXPLORE_RESULT = {
    "facts": [{"fact": "dispatch parses jsonl", "file_line": "src/x.py:1"}],
    "truncated": False,
    "more_in": ["src/"],
}

CODE_RESULT = {
    "status": "complete",
    "files_changed": ["src/x.py"],
    "validation": {
        "commands": [
            {"cmd": "pytest -q", "exit_code": 0, "output_summary": "3 passed"},
        ],
        "passed": True,
    },
    "remaining_risks": "none",
}

TEST_RESULT = {
    "command": "pytest -q",
    "passed": 3,
    "failed": 1,
    "failures": [{"test": "test_x", "error": "AssertionError: boom"}],
}

REVIEW_RESULT = {
    "overall": "changes_requested",
    "severity_counts": {"high": 1, "medium": 0, "low": 2},
    "findings": [
        {
            "severity": "high",
            "file_line": "src/x.py:10",
            "summary": "bad thing",
            "impact": "breaks stuff",
            "suggested_fix": "fix it",
        },
    ],
}


def _wrap(payload_text: str) -> str:
    return (
        "Report prose before the block.\n"
        + RESULT_BEGIN + "\n"
        + payload_text + "\n"
        + RESULT_END + "\n"
    )


def test_marker_constants():
    assert dispatch_mod.RESULT_BEGIN == RESULT_BEGIN
    assert dispatch_mod.RESULT_END == RESULT_END


def test_verdict_schema_version_is_2():
    assert dispatch_mod.VERDICT_SCHEMA_VERSION == 2


def test_extract_result_empty_text_is_unavailable():
    assert dispatch_mod.extract_result("", "explore") == ("unavailable", None)


def test_extract_result_text_without_markers_is_unavailable():
    text = "a perfectly normal report with no marker block at all"
    assert dispatch_mod.extract_result(text, "explore") == ("unavailable", None)


@pytest.mark.parametrize("mode,payload", [
    ("explore", EXPLORE_RESULT),
    ("code", CODE_RESULT),
    ("test", TEST_RESULT),
    ("review", REVIEW_RESULT),
    ("resume", CODE_RESULT),
])
def test_extract_result_valid_block_per_mode(mode, payload):
    parse_state, result = dispatch_mod.extract_result(
        _wrap(json.dumps(payload)), mode)
    assert parse_state == "parsed"
    assert result == payload


def test_extract_result_invalid_json_is_unstructured():
    parse_state, result = dispatch_mod.extract_result(
        _wrap("{not valid json"), "explore")
    assert parse_state == "unstructured"
    assert result is None


@pytest.mark.parametrize("mode,payload", [
    # explore missing facts
    ("explore", {"truncated": False, "more_in": []}),
    # explore facts entry missing file_line
    ("explore", {"facts": [{"fact": "x"}], "truncated": False, "more_in": []}),
    # code missing validation.passed
    ("code", {
        "status": "complete",
        "files_changed": [],
        "validation": {"commands": []},
        "remaining_risks": "none",
    }),
    # test missing failures list
    ("test", {"command": "pytest -q", "passed": 1, "failed": 0}),
    # review missing severity_counts
    ("review", {"overall": "ok", "findings": []}),
    # resume reuses the code schema: missing remaining_risks
    ("resume", {
        "status": "complete",
        "files_changed": [],
        "validation": {"commands": [], "passed": True},
    }),
])
def test_extract_result_schema_violation_is_unstructured(mode, payload):
    parse_state, result = dispatch_mod.extract_result(
        _wrap(json.dumps(payload)), mode)
    assert parse_state == "unstructured"
    assert result is None


def test_extract_result_last_begin_wins():
    first = _wrap(json.dumps({"not": "schema"}))
    second = _wrap(json.dumps(EXPLORE_RESULT))
    text = first + "\ninterleaved prose\n" + second
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "parsed"
    assert result == EXPLORE_RESULT


def test_extract_result_last_begin_wins_even_when_invalid():
    first = _wrap(json.dumps(EXPLORE_RESULT))
    second = _wrap("{broken")
    text = first + "\ninterleaved prose\n" + second
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "unstructured"
    assert result is None


def test_extract_result_trailing_prose_after_end_is_parsed():
    text = _wrap(json.dumps(EXPLORE_RESULT)) + "\nExtra trailing prose.\n"
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "parsed"
    assert result == EXPLORE_RESULT


def test_extract_result_begin_without_end_is_unstructured():
    text = (
        "prose\n" + RESULT_BEGIN + "\n" + json.dumps(EXPLORE_RESULT) + "\n"
    )
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "unstructured"
    assert result is None
