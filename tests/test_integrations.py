"""Offline tests for the v0.5.0 integration layer (design D1 + D3).

RED suite for Batch 5:
- Legacy per-provider integrations are gone; ONE host-agnostic playbook
  skill per host carries the full doctrine checklist from design D1.
- Every mode prompt under prompts/ instructs the worker to end with a
  literal PILOT_RESULT block carrying the mode's result schema (design D3);
  common.md stays block-free (the template is per-mode, not doubled).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot_workers import policy
from pilot_workers.cli import install as install_mod

INTEGRATIONS_DIR = install_mod.INTEGRATIONS_DIR
PROMPTS_DIR = policy.PROMPTS_DIR


# ----------------------------------------------------------------------
# D1 — legacy integration removal
# ----------------------------------------------------------------------

LEGACY_DIRS = (
    "claude-host/agents",
    "claude-host/commands",
    "codex-host/glm",
    "codex-host/kimi",
    "codex-host/ds",
)


@pytest.mark.parametrize("rel", LEGACY_DIRS)
def test_legacy_integration_dir_removed(rel):
    assert not (INTEGRATIONS_DIR / rel).exists(), (
        f"legacy integration path still present: {rel}"
    )


# ----------------------------------------------------------------------
# D1 — one non-stub playbook skill per host
# ----------------------------------------------------------------------

SKILL_ANCHORS = (
    "pilot-workers dispatch",
    "fanout",
    "verdict",
    "result",
    "parse_state",
    "final_text_path",
    "--worktree",
    "resume",
    "--run-id",
    # The routing-is-a-mandate doctrine: `install ... for <mode>` is the user
    # making the delegation decision once; per-task worth-it re-judgment is
    # what let a routed explore be silently done in-session.
    "not yours to remake",
    "background",
    "main session",
    "PILOT_RESULT",
)


MODES = ("explore", "code", "test", "review", "resume")


def _skill_dir(host: str):
    return INTEGRATIONS_DIR / host / "skills" / "pilot-workers"


def _skill_tree_text(host: str) -> str:
    """Core SKILL.md plus every modes/*.md — doctrine that moved into a child
    file still counts as present, but only if the file actually ships."""
    parts = [(_skill_dir(host) / "SKILL.md").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8")
              for p in sorted(_skill_dir(host).glob("modes/*.md"))]
    return "\n".join(parts)


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_playbook_skill_present_and_non_stub(host):
    path = _skill_dir(host) / "SKILL.md"
    assert path.is_file(), f"missing playbook skill: {path}"
    raw = path.read_bytes()
    assert len(raw) > 4000, (
        f"{path} is a stub ({len(raw)} bytes); expected the full playbook"
    )
    text = raw.decode("utf-8")
    lower = text.lower()
    # Core anchors: doctrine every dispatch needs regardless of mode, so it
    # must be in the always-loaded file, not a child.
    for anchor in SKILL_ANCHORS:
        assert anchor.lower() in lower, f"{path} missing doctrine anchor: {anchor!r}"
    tree = _skill_tree_text(host).lower()
    # Doctrine anchors with accepted spelling variants; these live in the
    # per-mode files after the split, so they are asserted on the whole tree.
    # NOT "spot-check": sampling was removed on purpose. A sample of N of M
    # conclusions licenses nothing about the other M-N and manufactures
    # confidence in them. What must survive is verification of the set actually
    # acted on — review findings — plus checks that are cheap AND complete.
    assert "verify every finding you intend to act on" in tree, (
        f"{host} missing verify-before-acting doctrine"
    )
    assert "diff --stat" in tree, (
        f"{host} missing the complete changed-file-set check"
    )
    assert ("cross-model" in tree) or ("different provider" in tree), (
        f"{host} missing cross-model review doctrine"
    )


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_each_mode_has_a_pointed_at_child_file(host):
    """Progressive disclosure: per-mode craft ships as modes/<mode>.md, and
    the core file must point at each one — an unreferenced child is doctrine
    the model will never know to read."""
    core = (_skill_dir(host) / "SKILL.md").read_text(encoding="utf-8")
    for mode in MODES:
        child = _skill_dir(host) / "modes" / f"{mode}.md"
        assert child.is_file(), f"{host}: missing modes/{mode}.md"
        assert len(child.read_text(encoding="utf-8")) > 300, (
            f"{host}: modes/{mode}.md is a stub")
        assert f"modes/{mode}.md" in core or "modes/<mode>.md" in core, (
            f"{host}: core never points at modes/{mode}.md")


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_core_skill_divides_reasoning_by_level(host):
    """The division-of-reasoning contract: host owns architecture,
    abstraction decisions and arbitration; the code worker owns
    implementation choices inside its scope (reuse, local structure) and
    reports cross-module duplication instead of inventing abstractions;
    explore gathers evidence; test planning is conditional on the OUTER
    workflow having a test stage; only cheaply-verifiable work is
    dispatched; independent tasks parallelize via fanout."""
    core = " ".join((_skill_dir(host) / "SKILL.md")
                    .read_text(encoding="utf-8").split())
    assert "## Division of reasoning" in core, f"{host}: section missing"
    assert "integration-test planning WHEN the outer workflow has a test stage" in core, (
        f"{host}: test planning is not conditional on the outer workflow")
    assert "do not micro-spec implementation" in core, (
        f"{host}: host may still micro-spec implementation")
    assert "search-before-edit gate" in core, (
        f"{host}: worker-side reuse enforcement not mentioned")
    assert "fanout" in core, f"{host}: parallel orchestration missing"
    assert "merging the worktrees back is your integration step" in core, (
        f"{host}: parallel code integration responsibility missing")
    assert "Cross-module abstractions are not the worker's to invent" in core, (
        f"{host}: abstraction decisions no longer reserved for the host")
    assert "verification is far cheaper than its execution" in core, (
        f"{host}: the dispatch test (cheap verification) is missing")


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_explore_playbook_has_three_requirement_scoped_lenses(host):
    """The explore craft: three parallel lenses (flow / constraints /
    abstraction evidence), each bounded by the change at hand, with the
    requirement written out in full for a fast-but-weak worker."""
    text = " ".join((_skill_dir(host) / "modes" / "explore.md")
                    .read_text(encoding="utf-8").split())
    assert "Three standard lenses" in text, f"{host}: lenses missing"
    assert "abstraction evidence" in text, f"{host}: third lens missing"
    assert "scoped by THIS change's needs" in text, (
        f"{host}: the anti-degeneration bound is gone")
    assert "write the requirement in full" in text, (
        f"{host}: complete-spec discipline missing")


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_core_skill_carries_no_mode_sections(host):
    """The split's whole point is loading less on trigger; a Mode section
    left in (or creeping back into) the core file pays its cost on every
    load while duplicating the child."""
    core = (_skill_dir(host) / "SKILL.md").read_text(encoding="utf-8")
    assert "## Mode:" not in core, f"{host}: a mode section is back in core"


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_core_skill_maps_scenarios_to_modes(host):
    """The scenario table: user phrasings AND the model's own mid-task
    impulses mapped to modes, with the purpose (learn vs verify) boundary."""
    core = " ".join((_skill_dir(host) / "SKILL.md")
                    .read_text(encoding="utf-8").split())
    assert "探索" in core, f"{host}: no Chinese exploration phrasing in core"
    assert "by PURPOSE" in core, f"{host}: purpose boundary missing"
    assert "file:line" in core, f"{host}: verify-locally example missing"


def test_claude_skill_frontmatter_has_name():
    path = INTEGRATIONS_DIR / "claude-host" / "skills" / "pilot-workers" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "claude SKILL.md missing frontmatter"
    frontmatter = text.split("---", 2)[1]
    assert "name: pilot-workers" in frontmatter


# ----------------------------------------------------------------------
# D3 — per-mode worker output contract (PILOT_RESULT block)
# ----------------------------------------------------------------------

RESULT_BEGIN = "<!--PILOT_RESULT_BEGIN-->"
RESULT_END = "<!--PILOT_RESULT_END-->"

# Per-mode result schema keys (design D3). resume reuses the code schema.
MODE_SCHEMA_KEYS = {
    "explore": ("facts", "file_line", "truncated", "more_in"),
    "code": ("files_changed", "validation", "output_summary", "remaining_risks"),
    "test": ("command", "failures", "passed", "failed"),
    "review": ("overall", "severity_counts", "findings", "suggested_fix", "impact"),
    "resume": ("files_changed", "validation", "output_summary", "remaining_risks"),
}


def _mode_prompt_text(mode: str) -> str:
    # policy.load_prompt maps resume onto code.md; assert against the same
    # resolution so the resume contract is pinned wherever it lives.
    name = "code" if mode == "resume" else mode
    path = PROMPTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing mode prompt: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", sorted(MODE_SCHEMA_KEYS))
def test_mode_prompt_has_result_block_and_schema_keys(mode):
    text = _mode_prompt_text(mode)
    assert RESULT_BEGIN in text, f"{mode} prompt missing {RESULT_BEGIN}"
    assert RESULT_END in text, f"{mode} prompt missing {RESULT_END}"
    assert text.index(RESULT_BEGIN) < text.index(RESULT_END), (
        f"{mode} prompt has result markers in the wrong order"
    )
    for key in MODE_SCHEMA_KEYS[mode]:
        assert f'"{key}"' in text, (
            f"{mode} prompt result block missing schema key {key!r}"
        )


def test_common_prompt_exists_and_has_no_result_block():
    path = PROMPTS_DIR / "common.md"
    assert path.is_file(), "common.md missing"
    text = path.read_text(encoding="utf-8")
    assert RESULT_BEGIN not in text, (
        "common.md must not contain the result block (it is per-mode)"
    )
    assert RESULT_END not in text, (
        "common.md must not contain the result block (it is per-mode)"
    )


def test_no_host_template_hardcodes_its_own_trigger_condition():
    """Both templates must hand the whole trigger clause to the placeholder.

    The clause is generated from the host's routing, so any wording left in the
    template competes with it — and the first pass at this fix patched the claude
    template and missed the codex one, which phrased the same sentence
    differently. Sibling-miss number twelve; the guard costs four lines.
    """
    from pathlib import Path as _Path

    import pilot_workers
    from pilot_workers.cli import install as install_mod

    root = _Path(pilot_workers.__file__).resolve().parent / "integrations"
    offenders = []
    for host in ("claude", "codex"):
        path = root / f"{host}-host" / "skills" / "pilot-workers" / "SKILL.md"
        front = install_mod._frontmatter_block(
            path.read_text(encoding="utf-8")) or ""
        normalised = " ".join(front.split())
        assert install_mod.TRIGGER_PLACEHOLDER in normalised, (
            f"{host}: the template lost its trigger placeholder")
        for phrase in ("Trigger when the user names",
                       "asks to delegate",
                       "to a worker model —",
                       # The economics escape hatch: any worth-it language in
                       # the frontmatter licenses the model to re-judge a
                       # routed mode per task and do the work in-session,
                       # which is exactly the failure the routing exists to
                       # prevent. (The BODY may still say "worth delegating"
                       # about UNROUTED modes — this guard reads only the
                       # frontmatter.)
                       "worth delegating",
                       "worth handing",
                       "Not for small tweaks"):
            if phrase in normalised:
                offenders.append(f"{host}: still hardcodes {phrase!r}")
    assert not offenders, "; ".join(offenders)


