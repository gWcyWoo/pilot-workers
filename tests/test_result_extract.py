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
    # Counts must agree with the list: a result claiming findings it does not
    # list is now rejected, so this fixture spells out every one it counts.
    "severity_counts": {"high": 1, "medium": 0, "low": 1},
    "findings": [
        {
            "severity": "high",
            "file_line": "src/x.py:10",
            "summary": "bad thing",
            "impact": "breaks stuff",
            "suggested_fix": "fix it",
        },
        {
            "severity": "low",
            "file_line": "src/y.py:3",
            "summary": "small thing",
            "impact": "mild",
            "suggested_fix": "tidy it",
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


def test_extract_result_invalid_json_is_malformed():
    """A block that is there but does not parse is NOT the same as no block.

    One unescaped quote (a worker quoting JSON inside a string field) used to
    read identically to a worker that ignored the contract, so the planner had
    no signal that a complete report was waiting in final_text_path.
    """
    parse_state, result = dispatch_mod.extract_result(
        _wrap("{not valid json"), "explore")
    assert parse_state == "malformed"
    assert result is None


def test_a_quoted_json_snippet_inside_a_field_is_malformed_not_missing():
    """The real shape of the slip: `"status": "x"` quoted inside a summary."""
    broken = ('{"facts": [{"fact": "a worker emitting `"status": "x"` fails",'
              ' "file_line": "a.py:1"}], "truncated": false, "more_in": []}')
    parse_state, result = dispatch_mod.extract_result(_wrap(broken), "explore")
    assert parse_state == "malformed"
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
def test_extract_result_schema_violation_is_malformed(mode, payload):
    parse_state, result = dispatch_mod.extract_result(
        _wrap(json.dumps(payload)), mode)
    assert parse_state == "malformed"
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
    assert parse_state == "malformed"
    assert result is None


def test_extract_result_trailing_prose_after_end_is_parsed():
    text = _wrap(json.dumps(EXPLORE_RESULT)) + "\nExtra trailing prose.\n"
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "parsed"
    assert result == EXPLORE_RESULT


def test_extract_result_begin_without_end_is_unstructured():
    """A truncated block keeps `unstructured`: the report was cut off, which is
    a different diagnosis from a block that finished but did not parse."""
    text = (
        "prose\n" + RESULT_BEGIN + "\n" + json.dumps(EXPLORE_RESULT) + "\n"
    )
    parse_state, result = dispatch_mod.extract_result(text, "explore")
    assert parse_state == "unstructured"
    assert result is None


def test_the_doctrine_teaches_every_parse_state_the_code_can_emit():
    """A state the planner has never been told about is a state it cannot act on.

    `malformed` was introduced because a worker's own report proved the gap: it
    wrote a complete review whose JSON had one unescaped quote, and the planner
    could not tell that from a worker that ignored the contract entirely.
    """
    import re
    from pathlib import Path

    import pilot_workers

    root = Path(pilot_workers.__file__).resolve().parent
    source = (root / "cli" / "dispatch.py").read_text(encoding="utf-8")
    body = source[source.index("def extract_result"):]
    body = body[:body.index("\ndef ")]
    emitted = set(re.findall(r'return "([a-z]+)"', body)) | {"parsed"}
    assert emitted == {"parsed", "malformed", "unstructured", "unavailable"}, emitted

    for host in ("claude", "codex"):
        doctrine = (root / "integrations" / f"{host}-host" / "skills"
                    / "pilot-workers" / "SKILL.md").read_text(encoding="utf-8")
        for state in sorted(emitted):
            assert state in doctrine, f"{host} doctrine never mentions {state!r}"


def test_the_review_contract_warns_about_the_quote_that_breaks_the_block():
    """The root cause of a `malformed` review, stated where it can prevent one."""
    from pathlib import Path

    import pilot_workers

    contract = " ".join((Path(pilot_workers.__file__).resolve().parent
                         / "prompts" / "review.md")
                        .read_text(encoding="utf-8").split())
    assert "never put a raw double quote inside a string field" in contract.lower()


@pytest.mark.parametrize("status", ["complete", "partial", "blocked"])
def test_the_documented_code_statuses_are_accepted(status):
    payload = {
        "status": status,
        "files_changed": ["a.py"],
        "validation": {"commands": [], "passed": True},
        "remaining_risks": "none",
    }
    parse_state, result = dispatch_mod.extract_result(
        _wrap(json.dumps(payload)), "code")
    assert parse_state == "parsed"
    assert result["status"] == status


@pytest.mark.parametrize("status", ["whatever", "", "COMPLETE", "done"])
def test_an_undocumented_code_status_is_refused(status):
    """prompts/code.md documents `<complete|partial|blocked>` in the same
    notation review uses for severity — and review's enum IS enforced. A status
    the planner cannot interpret is no more useful than a missing one."""
    payload = {
        "status": status,
        "files_changed": [],
        "validation": {"commands": [], "passed": True},
        "remaining_risks": "none",
    }
    parse_state, result = dispatch_mod.extract_result(
        _wrap(json.dumps(payload)), "code")
    assert parse_state == "malformed"
    assert result is None


def test_a_review_result_with_an_empty_overall_is_refused():
    """The contract says every string field is non-empty; an empty verdict
    paragraph would reach the planner as a result containing nothing."""
    payload = {"overall": "   ", "severity_counts": {"high": 0, "medium": 0, "low": 0},
               "findings": []}
    parse_state, _ = dispatch_mod.extract_result(_wrap(json.dumps(payload)), "review")
    assert parse_state == "malformed"


# ----------------------------------------------------------------------
# A rejected block must say WHY.
#
# `parse_state: "malformed"` with `result: null` and nothing else cost a real
# finding in round 15: a worker omitted `suggested_fix` from its one finding, the
# planner read "malformed", shrugged, and never opened final_text_path. The state
# was correct; it just carried no reason, so nobody acted on it.
# ----------------------------------------------------------------------


def _sample_parsed(final_text=""):
    """Minimal `parsed` dict shaped like parse_jsonl's output."""
    return {
        "steps": 1,
        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                   "cache_read": 0, "cache_write": 0},
        "tool_errors": {"permission_denied": 0, "other": 0},
        "final_text": final_text,
        "has_error_event": False,
        "duration_s": None,
    }

def _wrap(block: str) -> str:
    return f"report text\n{dispatch_mod.RESULT_BEGIN}\n{block}\n{dispatch_mod.RESULT_END}\n"


def test_a_parsed_block_has_no_problem_to_report():
    good = json.dumps({
        "overall": "fine",
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "findings": [],
    })
    assert dispatch_mod.result_problem(_wrap(good), "review") is None


def test_the_real_round_15_shape_is_explained():
    """The exact payload that lost a finding: one finding, no suggested_fix."""
    payload = json.dumps({
        "overall": "the change set is mature",
        "severity_counts": {"high": 0, "medium": 1, "low": 0},
        "findings": [{"severity": "medium", "file_line": "install.py:1181",
                      "summary": "s", "impact": "i"}],
    })
    problem = dispatch_mod.result_problem(_wrap(payload), "review")
    assert problem is not None
    assert "does not match the review schema" in problem
    # The shape is what makes it actionable — the missing key is visible by
    # comparing this list to the contract.
    assert "first entry keys" in problem
    assert "suggested_fix" not in problem.split("first entry keys")[1]


def test_a_json_error_names_the_position():
    problem = dispatch_mod.result_problem(_wrap('{"overall": "he said "hi""}'),
                                          "review")
    assert "not valid JSON" in problem and "line" in problem


def test_a_missing_block_and_an_unclosed_block_are_distinguished():
    assert "no PILOT_RESULT block" in dispatch_mod.result_problem("nothing", "review")
    opened = f"text\n{dispatch_mod.RESULT_BEGIN}\n{{}}"
    assert "never closed" in dispatch_mod.result_problem(opened, "review")


def test_the_verdict_carries_the_reason():
    """End to end: the field a planner actually reads."""
    payload = json.dumps({
        "overall": "x",
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "findings": [{"severity": "low", "file_line": "a:1", "summary": "s",
                      "impact": "i"}],
    })
    verdict = dispatch_mod.build_verdict(
        run_id="r", provider="glm", runner="opencode", mode="review",
        parsed=_sample_parsed(final_text=_wrap(payload)),
        summary={"exit_code": 0}, jsonl_path="/tmp/r.jsonl",
        stderr_path=None, report_path="/tmp/r.md", step_cap=120)
    assert verdict["parse_state"] == "malformed"
    assert verdict["result"] is None
    assert "does not match the review schema" in verdict["parse_error"]


def test_a_good_verdict_has_a_null_reason():
    payload = json.dumps({
        "overall": "x",
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "findings": [],
    })
    verdict = dispatch_mod.build_verdict(
        run_id="r", provider="glm", runner="opencode", mode="review",
        parsed=_sample_parsed(final_text=_wrap(payload)),
        summary={"exit_code": 0}, jsonl_path="/tmp/r.jsonl",
        stderr_path=None, report_path="/tmp/r.md", step_cap=120)
    assert verdict["parse_state"] == "parsed"
    assert verdict["parse_error"] is None


def test_reparsing_a_resumed_attempt_reports_both_ids(tmp_path, capsys):
    """The round-22/23 reparse fix had no test through the reparse path: the stem
    split AND the `resume_run_id` threading both stayed green when reverted.
    kimi and glm reported it independently.

    A resumed attempt's jsonl is `<sandbox>+<attempt>.jsonl`, so reparsing it
    must report `run_id` = the attempt and `resume_run_id` = the sandbox — the id
    a further resume has to pass.
    """
    sandbox_id = "20260101T000000Z-aaaaaaa1"
    attempt_id = "20260102T000000Z-bbbbbbb2"
    jsonl = tmp_path / f"{sandbox_id}+{attempt_id}.jsonl"
    block = json.dumps({
        "overall": "x",
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "findings": [],
    })
    jsonl.write_text(json.dumps({
        "type": "text",
        "part": {"text": f"report\n{dispatch_mod.RESULT_BEGIN}\n{block}\n"
                         f"{dispatch_mod.RESULT_END}\n"},
    }) + "\n", encoding="utf-8")

    rc = dispatch_mod.main(["--reparse", str(jsonl), "--mode", "review"])
    assert rc == 0
    # reparse PRINTS the verdict (the on-disk verdict.json belongs to the live
    # path); the report is written either way.
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict["run_id"] == attempt_id, (
        "reparse reported the whole stem as run_id — an id no dispatch ever had")
    assert verdict["resume_run_id"] == sandbox_id, (
        "reparse dropped resume_run_id, so a further resume has nothing to pass")
    assert (tmp_path / f"{sandbox_id}+{attempt_id}.report.md").is_file()


def test_reparsing_a_cold_run_reports_one_id_twice(tmp_path, capsys):
    """Reverse assertion: with no `+` in the stem both ids are the run id."""
    run_id = "20260101T000000Z-ccccccc3"
    jsonl = tmp_path / f"{run_id}.jsonl"
    block = json.dumps({
        "overall": "x",
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "findings": [],
    })
    jsonl.write_text(json.dumps({
        "type": "text",
        "part": {"text": f"r\n{dispatch_mod.RESULT_BEGIN}\n{block}\n"
                         f"{dispatch_mod.RESULT_END}\n"},
    }) + "\n", encoding="utf-8")

    assert dispatch_mod.main(["--reparse", str(jsonl), "--mode", "review"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict["run_id"] == run_id
    assert verdict["resume_run_id"] == run_id
