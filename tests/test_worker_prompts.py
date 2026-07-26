"""The injected prompts must tell a worker how to work inside its permissions.

A read-only worker is denied `sed` and `awk` on purpose — both can execute
arbitrary commands from their own argument text. But nothing told the worker
that, nor what to use instead, so a review of a 1000-line diff burned 45 steps
hitting the wall twice and returned 69 characters.
"""

from __future__ import annotations

# NOTE: a receiver-side rule the sender is never told is a false-rejection
# machine — see test_the_review_contract_states_every_rule_the_validator_enforces.

import fnmatch
from pathlib import Path

import pytest

import pilot_workers
from pilot_workers import policy


PROMPTS = Path(pilot_workers.__file__).resolve().parent / "prompts"
READONLY_MODES = ("explore", "test", "review")


def _verdict(cmd: str, rules: dict[str, str]) -> str:
    """Last matching pattern wins, as the runner's config encodes it."""
    verdict = "deny"
    for pattern, value in rules.items():
        if fnmatch.fnmatch(cmd, pattern):
            verdict = value
    return verdict


@pytest.mark.parametrize("mode", READONLY_MODES)
def test_readonly_prompt_explains_how_to_read_a_large_file(mode):
    text = (PROMPTS / f"{mode}.md").read_text(encoding="utf-8")
    combined = text + (PROMPTS / "common.md").read_text(encoding="utf-8")
    assert "sed" in combined and "awk" in combined, (
        f"{mode}.md never mentions that sed/awk are denied")
    assert "head" in combined or "tail" in combined, (
        f"{mode}.md offers no allowed way to page through a large file")


def test_the_suggested_pagination_is_actually_permitted():
    """Guidance that names a denied command would be worse than none."""
    rules = policy.readonly_shell_permissions()
    for cmd in ("git diff | head -200",
                "git diff | tail -n +200 | head -200",
                "grep -n pattern file",
                "head -200 file",
                "tail -n +200 file"):
        assert _verdict(cmd, rules) == "allow", f"suggested command is denied: {cmd}"


def test_the_denied_tools_are_still_denied():
    """The guidance must not have been written by loosening the sandbox."""
    rules = policy.readonly_shell_permissions()
    for cmd in ("sed -n 1,50p file", "awk 'NR<50' file", "git diff > /tmp/out"):
        assert _verdict(cmd, rules) == "deny", f"read-only sandbox weakened: {cmd}"


def test_the_review_contract_states_every_rule_the_validator_enforces():
    """A receiver-side rule the sender is never told is a false-rejection machine.

    `_validate_review_result` requires `severity_counts` to tally the findings
    exactly and rejects blank strings; `prompts/review.md` is the ONLY contract a
    review worker sees. A worker that summarises its counts had its whole result
    discarded as unstructured for breaking a rule nobody stated.
    """
    import pilot_workers

    contract = (Path(pilot_workers.__file__).resolve().parent
                / "prompts" / "review.md").read_text(encoding="utf-8").lower()
    assert "tally" in contract or "match" in contract, (
        "review.md never states that severity_counts must equal the findings")
    assert "non-empty" in contract or "must not be empty" in contract, (
        "review.md never states that fields cannot be blank")


def test_the_review_contract_does_not_tell_the_worker_to_stop_early():
    """A cap on findings reads as a target.

    Telemetry from nine review rounds: the step cap is 120 and workers used
    11-33 steps, median 12 — they stop when the report looks presentable, not
    when the scope is covered. The contract must not help them.
    """
    import pilot_workers

    root = Path(pilot_workers.__file__).resolve().parent
    contract = (root / "prompts" / "review.md").read_text(encoding="utf-8").lower()
    assert "no cap on the number of findings" in contract
    assert "coverage ledger" in contract
    assert "not examined" in contract, "silence must be reportable, not invisible"
    assert "[unverified]" in contract, (
        "a worker that cannot run code must have a way to say so")
    for anti_pattern in ("no more than", "prefer 6", "at most 15"):
        assert anti_pattern not in contract, (
            f"the contract still tells the worker to stop: {anti_pattern!r}")


def test_the_review_contract_names_all_three_passes():
    """Enumerate -> deep-read -> self-critique. The third is the one models skip:
    no worker in nine rounds asked what it had not looked at."""
    import pilot_workers

    contract = (Path(pilot_workers.__file__).resolve().parent
                / "prompts" / "review.md").read_text(encoding="utf-8").lower()
    for pass_marker in ("enumerate", "deep-read", "self-critique"):
        assert pass_marker in contract, f"missing pass: {pass_marker}"


def test_the_review_template_asks_for_the_already_fixed_list():
    """Every round after the first pays to re-find what the last one fixed
    unless the task carries the list — and it must be marked as a de-duplication
    aid, or the worker treats it as a no-look zone."""
    import pilot_workers

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / "review.md").read_text(encoding="utf-8")
    assert "do not re-report" in template.lower()
    assert "not a protected list" in template.lower()


def test_the_review_contract_carries_the_measured_failure_modes():
    """Ten rounds of lessons belong in the shipped contract, not in one
    planner's hand-written task files — every review dispatch pays for the
    prompt and should get the benefit."""
    import pilot_workers

    # Whitespace-normalised: these clauses wrap, and matching raw text is how
    # an earlier version of this suite produced eight false failures.
    contract = " ".join((Path(pilot_workers.__file__).resolve().parent
                         / "prompts" / "review.md")
                        .read_text(encoding="utf-8").lower().split())
    # Each clause exists because a specific round was spent discovering it.
    assert "comments lie" in contract, "nothing warns that docstrings were wrong"
    assert "suggested fix is a claim" in contract, (
        "three proposed fixes were wrong; nothing warns about that")
    assert "newest code" in contract, (
        "every high in rounds 7-9 was inside the previous round's fix")
    assert "category is not a finding" in contract
    assert "smallest change" in contract, "nothing discourages redesign proposals"
    assert "no-look zone" in contract, (
        "the already-fixed list must not read as protected")


def test_the_review_template_asks_the_dispatcher_for_a_coverage_map():
    """What gets found is what was asked: enumerating the ground is the
    dispatcher's job, not something the worker should have to invent."""
    import pilot_workers

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / "review.md").read_text(encoding="utf-8")
    assert "# Coverage Map" in template
    lowered = template.lower()
    assert "dimensions" in lowered and "surfaces" in lowered
    assert "not examined" in lowered, (
        "the map must make out-of-scope a decision, not an omission")


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_the_host_doctrine_tells_the_planner_to_refute_a_finding(host):
    """A reviewer cannot run code, so its findings are readings. Three of them
    were plausible and wrong in one session; two of those had fixes that would
    have made things worse."""
    import pilot_workers

    skill = (Path(pilot_workers.__file__).resolve().parent / "integrations"
             / f"{host}-host" / "skills" / "pilot-workers"
             / "SKILL.md").read_text(encoding="utf-8")
    flat = " ".join(skill.split()).lower()
    assert "refute" in flat, f"{host} doctrine never says to argue against a finding"
    assert "cannot run code" in flat, (
        f"{host} doctrine does not say why a finding needs refuting")


@pytest.mark.parametrize("shipped", [
    "prompts/review.md",
    "prompts/common.md",
    "data/templates/review.md",
    "data/templates/code.md",
    "data/templates/explore.md",
    "data/templates/test.md",
    "integrations/claude-host/skills/pilot-workers/SKILL.md",
    "integrations/codex-host/skills/pilot-workers/SKILL.md",
])
def test_shipped_text_carries_no_reference_to_this_project(shipped):
    """These files install into other people's repositories.

    A lesson learned here is worth shipping; the anecdote it came from is not.
    "every high in rounds 7-9" means nothing in a project that has no rounds,
    and a worker can read "this change set" as the one it is reviewing — turning
    provenance into a false statement about its own task.
    """
    import re

    import pilot_workers

    text = (Path(pilot_workers.__file__).resolve().parent / shipped).read_text(
        encoding="utf-8")
    flat = " ".join(text.split())
    local = [
        r"round[s]? \d",                 # "rounds 7-9"
        r"\b(?:nine|ten|eleven) rounds",  # "across ten review rounds"
        r"this change set",              # ambiguous: ours, or the one under review?
        r"median step",                  # a measurement of our own runs
        r"in one session",
    ]
    hits = [pattern for pattern in local
            if re.search(pattern, flat, re.IGNORECASE)]
    assert not hits, f"{shipped} refers to this project's own history: {hits}"


def test_the_worker_is_told_the_truth_about_credential_denies():
    """The deny matches a path, so a content search can still surface the bytes.

    Verified against a real worker: with the read tool refused, a recursive grep
    still returned a decoy line out of `.env`. Telling the worker the file is
    unreachable would be a false guarantee, and the worker would then have no
    reason to withhold what it stumbled on.
    """
    import pilot_workers

    common = " ".join((Path(pilot_workers.__file__).resolve().parent
                       / "prompts" / "common.md").read_text(encoding="utf-8").split())
    assert "cannot stop a content search" in common
    assert "do not repeat it in your report" in common.lower()


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_the_planner_is_told_what_the_workdir_actually_exposes(host):
    """`--worktree` is the real boundary (tracked files only — a gitignored
    `.env` is simply not there); the path denies are not."""
    import pilot_workers

    skill = " ".join((Path(pilot_workers.__file__).resolve().parent / "integrations"
                      / f"{host}-host" / "skills" / "pilot-workers" / "SKILL.md")
                     .read_text(encoding="utf-8").split())
    assert "can see the whole workdir" in skill.lower() or (
        "can also see the whole workdir" in skill.lower())
    assert "materialises tracked files only" in skill


def test_the_status_vocabulary_matches_what_the_validator_accepts():
    """common.md told every worker "complete, incomplete, or blocked" while the
    validator accepts only complete/partial/blocked, so a worker that followed
    the shared prompt emitted a result block the verdict rejected.

    No test LAYER catches a mismatch between a prompt and a constant — the two
    artifacts are each internally consistent. A consistency assertion does.
    """
    import re

    from pilot_workers import policy
    from pilot_workers.cli.dispatch import CODE_STATUSES

    common = (policy.PROMPTS_DIR / "common.md").read_text(encoding="utf-8")
    line = next(l for l in common.splitlines() if "`STATUS`" in l)
    named = set(re.findall(r"\b(?:complete|incomplete|partial|blocked)\b", line))
    assert named == set(CODE_STATUSES), (
        f"common.md names {sorted(named)}, the validator accepts "
        f"{sorted(CODE_STATUSES)}")


def test_the_mode_prompt_agrees_with_the_shared_prompt():
    """code.md spells the same vocabulary in its JSON block."""
    from pilot_workers import policy
    from pilot_workers.cli.dispatch import CODE_STATUSES

    code_md = (policy.PROMPTS_DIR / "code.md").read_text(encoding="utf-8")
    for status in CODE_STATUSES:
        assert status in code_md, f"code.md never mentions {status!r}"
    assert "incomplete" not in code_md
