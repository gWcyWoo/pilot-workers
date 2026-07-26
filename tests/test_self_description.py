"""The tool must describe itself accurately and name what the user got wrong.

A generic usage dump makes the operator diff their command against a wall of
text; naming the offending token is the difference between a two-second fix and
a hunt. And `--help` is a question, not a mistake: it belongs on stdout with
exit 0.
"""

from __future__ import annotations

import pytest

from pilot_workers.cli import install as install_mod
from pilot_workers.cli import main as main_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return {"home": tmp_path / "home", "target": tmp_path / "target"}


# ----------------------------------------------------------------------
# --help is a question
# ----------------------------------------------------------------------


@pytest.mark.parametrize("sub", ["status", "template", "install", "uninstall",
                                 "dispatch", "fanout"])
def test_help_exits_zero_on_stdout(sub, capsys):
    """argparse-backed subcommands raise SystemExit(0); hand-rolled ones return
    0. Either is a success — what must never happen is exit 2 (a usage error) or
    the text landing on stderr."""
    try:
        rc = main_mod.main([sub, "--help"])
    except SystemExit as exc:
        rc = exc.code or 0
    assert rc == 0, f"{sub} --help is reported as a failure"
    out, _ = capsys.readouterr()
    assert out.strip(), f"{sub} --help printed nothing to stdout"
    assert "usage" in out.lower()


@pytest.mark.parametrize("sub,flag", [("dispatch", "--provider"),
                                      ("fanout", "--workdir")])
def test_help_does_not_present_a_required_flag_as_optional(sub, flag, capsys):
    """Both use argparse defaults that make a required flag look optional, so
    `--help` invited a command that then failed with exit 2."""
    try:
        main_mod.main([sub, "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    # argparse wraps and re-lays-out, so pin the CONCEPT: somewhere in the help
    # for this flag it must say the flag is required. Matching one line was
    # brittle — the usage line and the description block are far apart.
    body = " ".join(out.split())
    i = body.rfind(flag)
    assert i >= 0, f"{sub} --help never mentions {flag}"
    assert "required" in body[i:i + 200].lower(), (
        f"{sub} --help presents {flag} as optional: {body[i:i + 120]!r}")


@pytest.mark.parametrize("sub,flag,rest", [
    ("dispatch", "--provider", ["--mode", "review", "--workdir", ".",
                                "--task", "x"]),
    ("fanout", "--workdir", ["--job", "glm:review:/tmp/nope.md"]),
])
def test_omitting_a_required_flag_actually_fails(sub, flag, rest, capsys):
    """The help text above is a promise; this is the behaviour behind it.

    Both commands deliberately skip argparse's own ``required=True`` (to control
    the message), so a test that only reads the help string stays green if the
    runtime check is deleted.
    """
    rc = main_mod.main([sub, *rest])
    assert rc != 0, f"{sub} accepted a command with no {flag}"
    captured = capsys.readouterr()
    assert flag in (captured.err + captured.out), (
        f"{sub} refused the command without naming {flag}")


# ----------------------------------------------------------------------
# advertised == accepted
# ----------------------------------------------------------------------


def test_install_usage_advertises_global_key():
    """The parser accepts it and the top-level usage lists it; the subcommand's
    own help omitted it, which is where a user actually looks."""
    assert "--global-key" in install_mod.INSTALL_USAGE


# ----------------------------------------------------------------------
# errors name the offending value
# ----------------------------------------------------------------------


def test_unknown_provider_is_named(isolated, capsys):
    assert install_mod.main(["glmm", "on", "claude"]) == 2
    err = capsys.readouterr().err
    assert "glmm" in err, "the error never says which token was wrong"
    assert "glm" in err, "no hint at the valid keys"


def test_unknown_host_is_named(isolated, capsys):
    assert install_mod.main(["glm", "on", "clod"]) == 2
    err = capsys.readouterr().err
    assert "clod" in err
    assert "claude" in err, "no hint at the valid hosts"


def test_unknown_mode_is_named(isolated, capsys):
    assert install_mod.main(["glm", "on", "claude", "for", "bogus"]) == 1
    err = capsys.readouterr().err
    assert "bogus" in err


def test_a_known_provider_with_the_wrong_shape_names_the_missing_element(
        isolated, capsys):
    """`install glm for code` and `install glm onto claude` used to print the
    whole grammar and leave the operator to diff their command against it."""
    assert install_mod.main(["glm", "onto", "claude"]) == 2
    err = capsys.readouterr().err
    assert "on <host>" in err and "glm" in err
    assert "usage:" not in err, "the wall of usage is what this replaced"

    assert install_mod.main(["glm", "for", "code"]) == 2
    assert "on <host>" in capsys.readouterr().err


def test_a_mode_assignment_without_a_provider_names_the_provider(isolated, capsys):
    """`install for code on claude` is the assignment shape minus the one word
    the command exists to record."""
    assert install_mod.main(["for", "code", "on", "claude"]) == 2
    err = capsys.readouterr().err
    assert "no provider named" in err
    assert "<provider> on claude for code" in err


def test_a_genuinely_malformed_command_still_gets_usage(isolated, capsys):
    """Naming values must not swallow the fallback for shapes we cannot parse."""
    assert install_mod.main(["a", "b", "c", "d", "e", "f"]) == 2
    assert "usage:" in capsys.readouterr().err


# ----------------------------------------------------------------------
# every state can be undone
# ----------------------------------------------------------------------


def test_a_credential_can_be_removed(isolated, monkeypatch, capsys):
    """`--global-key` writes a key; nothing could remove it without hand-editing
    a file under a dot-directory."""
    from pilot_workers import providers
    from pilot_workers.runners import get_runner

    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "fake-key-value-xyz")
    assert install_mod.main(
        ["glm", "on", "claude", "--global-key",
         "--target", str(isolated["target"])]) == 0

    provider = providers.PROVIDERS["glm"]
    path = get_runner(provider.runner).credential_path(provider)
    assert path.is_file()

    assert install_mod.uninstall_main(["key", "glm"]) == 0
    assert not path.exists(), "the credential survived its removal command"


def test_removing_an_absent_credential_is_not_an_error(isolated, capsys):
    assert install_mod.uninstall_main(["key", "glm"]) == 0
    assert "no" in capsys.readouterr().out.lower()


def test_removing_a_credential_for_an_unknown_provider_is_named(isolated, capsys):
    assert install_mod.uninstall_main(["key", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_key_is_a_reserved_provider_key():
    """`uninstall key <provider>` made `key` a grammar keyword, so a provider
    YAML claiming that name would make the command ambiguous."""
    from pilot_workers import providers
    assert "key" in providers.RESERVED_PROVIDER_KEYS


def test_key_on_host_is_not_read_as_provider_removal(isolated, capsys):
    """`uninstall key on claude` was classified as removing a provider named
    'key': it exited 0 having removed nothing, so the user believed their
    credential was gone."""
    rc = install_mod.uninstall_main(["key", "on", "claude"])
    assert rc == 2, "a malformed key command reported success"
    assert "usage" in capsys.readouterr().err.lower()


def test_usage_lists_the_credential_removal_command():
    """`uninstall key <provider>` is the only way to remove a credential, and it
    was missing from the first screen a user sees."""
    from pilot_workers.cli import main as main_module
    assert "uninstall key <provider>" in main_module.USAGE


def test_unknown_provider_is_named_in_the_for_mode_form(isolated, capsys):
    """Naming only covered the 3-token shape, so the longer form fell back to a
    usage wall — the form a user is MORE likely to typo, being longer."""
    assert install_mod.main(["glmm", "on", "claude", "for", "code"]) == 2
    err = capsys.readouterr().err
    assert "glmm" in err and "unknown provider" in err


def test_unknown_host_is_named_in_the_for_mode_form(isolated, capsys):
    assert install_mod.main(["glm", "on", "clod", "for", "code"]) == 2
    err = capsys.readouterr().err
    assert "clod" in err and "unknown host" in err


def test_both_host_skills_tell_the_planner_to_resume_off_resume_run_id():
    """The skills said "--run-id <run_id from the verdict>", which is the one
    field a resumed run reports that cannot be resumed with. A doctrine that
    names the wrong field is the defect, not a typo: the planner follows it."""
    from pathlib import Path

    import pilot_workers

    integrations = (Path(pilot_workers.__file__).resolve().parent / "integrations")
    for host in ("claude", "codex"):
        path = integrations / f"{host}-host" / "skills" / "pilot-workers" / "SKILL.md"
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "resume_run_id" in text, f"{host}: does not mention resume_run_id"
        assert "--run-id <run_id from the verdict>" not in text, (
            f"{host}: still points the planner at run_id")


def test_the_field_the_skills_name_is_the_field_the_verdict_carries():
    """Doctrine and payload must agree — a skill naming a field the verdict does
    not emit is worse than one naming the wrong field, because it looks fixed."""
    from pilot_workers.cli.dispatch import build_verdict

    verdict = build_verdict(
        run_id="new-id", resume_run_id="sandbox-id", provider="glm",
        runner="opencode", mode="resume",
        parsed={
            "steps": 1,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache_read": 0, "cache_write": 0},
            "tool_errors": {"permission_denied": 0, "other": 0},
            "final_text": "x",
            "has_error_event": False,
            "duration_s": None,
        },
        summary={"exit_code": 0},
        jsonl_path="/tmp/x.jsonl", stderr_path=None, report_path="/tmp/x.md",
        step_cap=120,
    )
    assert verdict["resume_run_id"] == "sandbox-id"
    assert verdict["run_id"] == "new-id"


def test_every_document_that_lists_parse_state_lists_all_of_them():
    """The enumeration guard for the class that produced three sibling-misses.

    A vocabulary is defined in code and then RESTATED in prose — the shared
    prompt, the architecture doc, both host skills. Three times this session a
    restatement was stale: common.md named a STATUS value the validator rejects,
    it claimed sed/awk are denied "in every mode" when code mode allows them, and
    architecture.md listed parse_state twice and only one copy was fixed.

    Scoped to DECLARATIVE forms and run on whitespace-normalised text. The first
    version scanned line by line and flagged two false positives: a four-value
    list that happens to wrap across four lines, and a table cell that mentions
    two states while describing something else. A guard that cries wolf gets
    switched off, which is worse than no guard.
    """
    import re
    from pathlib import Path as _Path

    import pilot_workers

    STATES = ["parsed", "malformed", "unstructured", "unavailable"]
    root = _Path(pilot_workers.__file__).resolve().parent
    docs = [root.parent.parent / "docs" / "architecture.md",
            root.parent.parent / "CLAUDE.md",
            root / "integrations" / "claude-host" / "skills" / "pilot-workers" / "SKILL.md",
            root / "integrations" / "codex-host" / "skills" / "pilot-workers" / "SKILL.md"]
    # Only the two shapes that DECLARE the vocabulary.
    declarations = (
        re.compile(r"`parse_state`\s*(?:=|:)\s*((?:[^.]|\.(?!\s))*)"),
        re.compile(r"`parse_state`\s+values:\s*((?:[^.]|\.(?!\s))*)"),
    )

    offenders = []
    for path in docs:
        if not path.is_file():
            continue
        text = " ".join(path.read_text(encoding="utf-8").split())
        for pattern in declarations:
            for match in pattern.finditer(text):
                span = match.group(1)[:400]
                named = [s for s in STATES if re.search(rf"\b{s}\b", span)]
                if len(named) >= 2 and len(named) != len(STATES):
                    offenders.append(
                        f"{path.name}: a declaration names {named}, missing "
                        f"{[s for s in STATES if s not in named]}")
    assert not offenders, "; ".join(offenders)


def test_the_guard_would_notice_a_missing_state():
    """Reverse assertion: the guard's rule must flag the exact line it exists for."""
    import re

    STATES = ["parsed", "malformed", "unstructured", "unavailable"]
    text = "- `parse_state`: `parsed` | `unstructured` | `unavailable` — whether"
    match = re.search(r"`parse_state`\s*(?:=|:)\s*((?:[^.]|\.(?!\s))*)", text)
    assert match
    named = [s for s in STATES if re.search(rf"\b{s}\b", match.group(1))]
    assert len(named) >= 2 and len(named) != len(STATES), (
        "the guard would not flag the stale line it was written for")
