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
    "spec",          # worth-it self-check
    "background",
    "main session",
    "PILOT_RESULT",
)


@pytest.mark.parametrize("host", ["claude-host", "codex-host"])
def test_playbook_skill_present_and_non_stub(host):
    path = INTEGRATIONS_DIR / host / "skills" / "pilot-workers" / "SKILL.md"
    assert path.is_file(), f"missing playbook skill: {path}"
    raw = path.read_bytes()
    assert len(raw) > 4000, (
        f"{path} is a stub ({len(raw)} bytes); expected the full playbook"
    )
    text = raw.decode("utf-8")
    lower = text.lower()
    for anchor in SKILL_ANCHORS:
        assert anchor.lower() in lower, f"{path} missing doctrine anchor: {anchor!r}"
    # Doctrine anchors with accepted spelling variants.
    assert ("spot-check" in lower) or ("spot check" in lower), (
        f"{path} missing spot-check doctrine"
    )
    assert ("cross-model" in lower) or ("different provider" in lower), (
        f"{path} missing cross-model review doctrine"
    )


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
