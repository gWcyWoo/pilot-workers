"""Hard checks on the task text, replacing template comments nobody enforced.

The task file is sent verbatim to a third-party model endpoint, so a secret in it
is exfiltrated. The template used to *ask* the author not to include credentials;
asking is not a control. These are the controls.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from pilot_workers import taskguard


# ----------------------------------------------------------------------
# a configured provider's real key must never leave the machine
# ----------------------------------------------------------------------


def test_exact_configured_key_is_refused():
    secret = "sk-abcdef0123456789abcdef"
    task = f"Call the API with {secret} and report the result."
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(task, known_secrets=[secret])


def test_key_is_never_echoed_in_the_error():
    secret = "sk-abcdef0123456789abcdef"
    with pytest.raises(RuntimeError) as excinfo:
        taskguard.check_task(f"use {secret}", known_secrets=[secret])
    assert secret not in str(excinfo.value)


def test_a_task_without_the_key_passes():
    taskguard.check_task("Refactor the parser.", known_secrets=["sk-secret-value"])


def test_empty_known_secrets_does_not_crash():
    taskguard.check_task("Refactor the parser.", known_secrets=[])


def test_short_secrets_are_ignored():
    """A 3-char 'secret' would match ordinary prose; only real keys are scanned."""
    taskguard.check_task("abc is fine here", known_secrets=["abc"])


# ----------------------------------------------------------------------
# generic credential shapes, for keys this machine does not hold
# ----------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "AKIAIOSFODNN7EXAMPLE",
    "export ANTHROPIC_API_KEY=sk-ant-api03-Xy9largeenoughtoken",
])
def test_generic_credential_shapes_are_refused(text):
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"Here is context:\n{text}\n", known_secrets=[])


@pytest.mark.parametrize("text", [
    "Set the api_key from the environment, never inline.",
    "The token bucket refills every second.",
    "Read AWS_SECRET_ACCESS_KEY from the env at runtime.",
    "password reset flow needs a test",
])
def test_prose_about_credentials_is_allowed(text):
    """Refusing the word 'token' would make the guard useless in practice."""
    taskguard.check_task(text, known_secrets=[])


# ----------------------------------------------------------------------
# unfilled template placeholders
# ----------------------------------------------------------------------


def test_unfilled_template_comment_is_refused():
    task = ("# Objective\n\n"
            "<!--PILOT_FILL Observable completion-result checklist -->\n")
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(task, known_secrets=[])


def test_a_filled_task_with_ordinary_html_comment_passes():
    """Only the template's own placeholder shape is rejected."""
    taskguard.check_task(
        "# Objective\n\nMake the tests pass.\n<!--PILOT_RESULT_BEGIN-->\n",
        known_secrets=[])


@pytest.mark.parametrize("comment", [
    "<!-- pilot-workers task · review mode (read-only) -->",
    "<!-- reviewer: ignore the vendored dir -->",
    "<!-- TODO after this lands: bump the version -->",
])
def test_hand_written_comments_are_not_placeholders(comment):
    """A hand-written task may carry ordinary comments.

    Treating every HTML comment as an unfilled placeholder refused real tasks —
    it blocked three review dispatches whose only comment was a mode banner.
    Placeholders are marked, so the check can recognise exactly those.
    """
    taskguard.check_task(f"{comment}\n\n# Objective\n\nReal content.\n",
                         known_secrets=[])


def test_marked_placeholder_is_still_refused():
    taskguard.check_task  # anchor
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(
            "# Objective\n\n<!--PILOT_FILL what to achieve -->\n",
            known_secrets=[])


def test_error_names_which_check_failed():
    with pytest.raises(RuntimeError, match="unfilled template"):
        taskguard.check_task("<!--PILOT_FILL fill me in -->", known_secrets=[])


# ----------------------------------------------------------------------
# wired into the dispatch path, before anything leaves the machine
# ----------------------------------------------------------------------


def test_run_refuses_a_task_carrying_a_credential(tmp_path, monkeypatch, capsys):
    from pilot_workers.cli import run as run_mod

    task = tmp_path / "t.md"
    task.write_text("Use -----BEGIN RSA PRIVATE KEY----- to sign.", encoding="utf-8")
    rc = run_mod.main(["--provider", "glm", "--mode", "code",
                       "--workdir", str(tmp_path), "--task-file", str(task)])
    assert rc == 1
    assert "credential" in capsys.readouterr().err


def test_run_refuses_before_touching_the_provider(tmp_path, capsys):
    """The guard must run before any credential load or sandbox setup."""
    from pilot_workers.cli import run as run_mod

    task = tmp_path / "t.md"
    task.write_text("<!--PILOT_FILL unfilled -->", encoding="utf-8")
    rc = run_mod.main(["--provider", "glm", "--mode", "code",
                       "--workdir", str(tmp_path), "--task-file", str(task)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unfilled" in err
    # A missing-credential error would mean the guard ran too late.
    assert "credential missing" not in err


def test_run_accepts_a_clean_task(tmp_path, capsys):
    """Sanity: the guard must not reject ordinary work (dry-run, no model call)."""
    from pilot_workers.cli import run as run_mod

    task = tmp_path / "t.md"
    task.write_text("Rename the parser helper and update its callers.",
                    encoding="utf-8")
    rc = run_mod.main(["--provider", "glm", "--mode", "code",
                       "--workdir", str(tmp_path), "--task-file", str(task),
                       "--dry-run"])
    assert rc == 0
    assert "credential" not in capsys.readouterr().err


# ----------------------------------------------------------------------
# the template must survive its own guard
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_filled_template_passes_the_guard(mode):
    """Keeping the header while filling the sections must not be refused.

    The header is an HTML comment addressed to the author, so the placeholder
    check would flag it — a guard that rejects correct usage teaches people to
    route around it.
    """
    import pilot_workers
    from pathlib import Path

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8")
    header = template[:template.index("-->") + 3]

    taskguard.check_task(header + "\n\n# Section\n\nReal content here.\n",
                         known_secrets=[])


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_unfilled_template_is_still_refused(mode):
    """The whole point: dispatching the raw template must fail."""
    import pilot_workers
    from pathlib import Path

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(template, known_secrets=[])


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_template_no_longer_repeats_worker_side_discipline(mode):
    """Those lines live in prompts/*.md, which dispatch injects. A second copy
    in the template is paid for by the main session and drifts."""
    import pilot_workers
    from pathlib import Path

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8")
    assert "Never include any credentials" not in template, "now a hard check"
    assert "uncommitted changes" not in template, "lives in prompts/common.md"
    assert "STATUS / FILES_CHANGED" not in template, "lives in prompts/common.md"


# ----------------------------------------------------------------------
# task text must not reach the child's argv
# ----------------------------------------------------------------------


def test_dispatch_keeps_task_text_out_of_argv():
    """argv is readable by same-user processes via `ps`, and the project's own
    architecture note says task text never travels in argv. The builder now
    refuses to take inline text at all — the only way in is a file.
    """
    from pilot_workers.cli import dispatch as dispatch_mod

    with pytest.raises(AssertionError, match="materialised to a file"):
        dispatch_mod._build_runner_command(
            "glm", "code", "/tmp", "SENSITIVE-DETAIL", None, None, False, 60, 60)


def test_inline_task_becomes_a_private_file(tmp_path, monkeypatch):
    """`--task` is materialised at 0600 and handed over as --task-file."""
    from pilot_workers.cli import dispatch as dispatch_mod
    import os
    import stat as stat_mod

    seen: dict = {}
    real = dispatch_mod._build_runner_command

    def _spy(provider, mode, workdir, task, task_file, *rest, **kw):
        seen["task"] = task
        seen["path"] = task_file
        seen["mode_bits"] = stat_mod.S_IMODE(os.stat(task_file).st_mode)
        seen["content"] = open(task_file, encoding="utf-8").read()
        return real(provider, mode, workdir, task, task_file, *rest, **kw)

    monkeypatch.setattr(dispatch_mod, "_build_runner_command", _spy)
    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task", "SENSITIVE-DETAIL",
    ])
    dispatch_mod.run_dispatch(args)

    assert seen["task"] is None, "inline text must not reach the builder"
    assert seen["content"] == "SENSITIVE-DETAIL"
    assert seen["mode_bits"] == 0o600


def test_dispatch_still_forwards_an_explicit_task_file(tmp_path):
    from pilot_workers.cli import dispatch as dispatch_mod

    given = tmp_path / "t.md"
    given.write_text("from a file", encoding="utf-8")
    cmd = dispatch_mod._build_runner_command(
        "glm", "code", "/tmp", None, str(given), None, False, 60, 60)
    assert cmd[cmd.index("--task-file") + 1] == str(given)


def test_dispatch_deletes_the_temp_task_file(tmp_path, monkeypatch):
    """A 0600 file holding the task must not outlive the run.

    Fixing the argv exposure by writing a temp file trades a transient exposure
    for a permanent one unless the file is removed.
    """
    from pilot_workers.cli import dispatch as dispatch_mod
    import glob
    import tempfile as tf

    before = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))

    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task", "inline task text",
    ])
    dispatch_mod.run_dispatch(args)

    after = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))
    assert after == before, f"left behind: {after - before}"


# ----------------------------------------------------------------------
# diagnostics must not misattribute the failure
# ----------------------------------------------------------------------


def test_pre_launch_refusal_does_not_blame_the_runner(tmp_path, capsys):
    """`run` can refuse before the runner is ever launched — a rejected task, a
    bad workdir. Reporting "runner never emitted ..." then sends the reader to
    the wrong component; it cost real debugging time when three review
    dispatches were refused by taskguard.
    """
    from pilot_workers.cli import dispatch as dispatch_mod

    task = tmp_path / "t.md"
    task.write_text("<!--PILOT_FILL not filled in -->", encoding="utf-8")
    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task-file", str(task),
    ])
    dispatch_mod.run_dispatch(args)

    err = capsys.readouterr().err
    assert "unfilled" in err, "the real cause must still be shown"
    assert "runner never emitted" not in err, (
        "blames the runner for a refusal that happened before launch")


# ----------------------------------------------------------------------
# the guard must be discussable in the tasks that discuss it
# ----------------------------------------------------------------------


def test_prose_mentioning_the_placeholder_marker_is_not_refused():
    """Describing the guard must not trip it.

    Round-2 review tasks were refused because they explained the mechanism to
    the reviewer, quoting the marker inline. Every task about this feature —
    reviews, docs, follow-up work — would hit the same wall.
    """
    task = (
        "# Questions\n\n"
        "The guard refuses a task that still holds an unfilled "
        "`<!--PILOT_FILL ...-->` placeholder. Assess whether that is right.\n"
    )
    taskguard.check_task(task, known_secrets=[])


def test_a_fenced_code_block_showing_the_marker_is_not_refused():
    task = (
        "# Questions\n\n"
        "The template looks like this:\n\n"
        "```\n"
        "# Objective\n"
        "<!--PILOT_FILL what to achieve -->\n"
        "```\n\n"
        "Is that shape right?\n"
    )
    taskguard.check_task(task, known_secrets=[])


def test_a_real_unfilled_placeholder_is_still_refused():
    """The actual case: a template placeholder left in place, at line start."""
    task = "# Objective\n\n<!--PILOT_FILL what to achieve -->\n"
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(task, known_secrets=[])


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_every_packaged_template_is_still_refused_raw(mode):
    import pilot_workers

    template = (Path(pilot_workers.__file__).resolve().parent
                / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(template, known_secrets=[])


# ----------------------------------------------------------------------
# the template must not restate what dispatch injects for free
# ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["explore", "review", "test", "code"])
def test_template_does_not_restate_injected_output_rules(mode):
    """`prompts/<mode>.md` is injected into the worker at no cost to the planner.
    A template section repeating those rules is paid for by the planner, who
    types it, and drifts from the copy that actually reaches the worker.
    """
    import pilot_workers

    root = Path(pilot_workers.__file__).resolve().parent
    template = (root / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8")

    assert "# Output Discipline" not in template, (
        "output rules belong in prompts/, which dispatch injects")
    for phrase in ("no preamble", "at most 3 lines"):
        assert phrase not in template.lower(), f"{phrase!r} duplicates prompts/"


def _instruction_lines(text: str) -> set[str]:
    """Substantial instruction lines, normalised past markup and wrapping."""
    lines = set()
    for raw in text.splitlines():
        line = re.sub(r"^[\s>*+\-0-9.]+", "", raw).strip().lower()
        line = re.sub(r"[`*_]", "", line)
        line = re.sub(r"\s+", " ", line)
        if len(line) >= 40:
            lines.add(line)
    return lines


@pytest.mark.parametrize("mode", ["code", "explore", "test", "review"])
def test_no_instruction_line_is_duplicated_between_template_and_prompt(mode):
    """The phrase pins above only catch the wordings we already deleted.

    This catches ANY re-duplication: a line the planner types into the task
    that the worker is also told directly is paid for twice and drifts. Compared
    on normalised text, so re-wrapping or re-bulleting does not hide it.
    """
    import pilot_workers

    root = Path(pilot_workers.__file__).resolve().parent
    injected = _instruction_lines(
        (root / "prompts" / "common.md").read_text(encoding="utf-8")
        + (root / "prompts" / f"{mode}.md").read_text(encoding="utf-8"))
    template = _instruction_lines(
        (root / "data" / "templates" / f"{mode}.md").read_text(encoding="utf-8"))

    assert not (template & injected), (
        f"{mode}: duplicated between the template and the injected prompt: "
        f"{sorted(template & injected)}")


@pytest.mark.parametrize("mode", ["explore", "review", "test", "code"])
def test_the_injected_prompt_still_carries_those_rules(mode):
    """Deleting from the template only helps if the rules survive elsewhere."""
    import pilot_workers

    root = Path(pilot_workers.__file__).resolve().parent
    injected = ((root / "prompts" / f"{mode}.md").read_text(encoding="utf-8")
                + (root / "prompts" / "common.md").read_text(encoding="utf-8"))
    assert "PILOT_RESULT_BEGIN" in injected
    assert "preamble" in injected.lower() or "verbatim" in injected.lower()


@pytest.mark.parametrize("secret", [
    "github_pat_11ABCDEFG0abcdefghij_XYZ123456789abcdefghijklmn",
    "glpat-ABCdef123456789_xyz",
    "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567",
    "xoxb-1234567890-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
])
def test_more_credential_shapes_are_refused(secret):
    """Each of these is a real, current key format that was being dispatched
    verbatim to a third-party endpoint."""
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"authenticate with {secret} please", known_secrets=[])


@pytest.mark.parametrize("text", [
    "the github_pat naming convention is confusing",
    "glpat tokens are a GitLab thing",
    "we should document AIza-style keys somewhere",
    "a JWT has three dot-separated parts",
])
def test_prose_about_those_shapes_is_allowed(text):
    taskguard.check_task(text, known_secrets=[])


def test_a_tilde_fenced_block_showing_the_marker_is_not_refused():
    """Markdown allows ~~~ fences as well as backticks."""
    task = ("# Questions\n\n"
            "~~~\n"
            "<!--PILOT_FILL what to achieve -->\n"
            "~~~\n\n"
            "Is that shape right?\n")
    taskguard.check_task(task, known_secrets=[])


@pytest.mark.parametrize("secret", [
    "sk_live_51ABCdefGHIjklMNOpqrSTUvwx",
    "sk_test_51ABCdefGHIjklMNOpqrSTUvwx",
    "rk_live_51ABCdefGHIjklMNOpqrSTUvwx",
    "AKIA" + "A" * 16,
])
def test_underscore_prefixed_keys_are_refused(secret):
    """The `sk-` shape required a hyphen, so Stripe's underscore form — a live
    payments credential — passed straight through to a third-party endpoint."""
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"charge the card with {secret}", known_secrets=[])


@pytest.mark.parametrize("secret", [
    "HF_TOKEN=hf_QwErTyUiOpAsDfGhJkLzXcVb",
    "hf_qwertyuiopasdfghjklzxcvb",
    "GROQ_API_KEY=gsk_abcd1234ef5678gh90ijklmn",
    "gsk_abcd1234ef5678gh90ijklmn",
    "REPLICATE_API_TOKEN=r8_abcd1234ef5678gh90ijklmn",
])
def test_model_vendor_tokens_are_refused(secret):
    """Every one of these bypassed all shapes.

    The assignment shape enumerated six key NAMES (so `HF_TOKEN=` missed) and
    demanded an uppercase char in the VALUE (so lowercase-hex keys missed) —
    which is the shape of most model-hosting credentials.
    """
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"call the endpoint with {secret}", known_secrets=[])


@pytest.mark.parametrize("secret", [
    "AWS_SECRET_ACCESS_KEY=AbCd1234EfGh5678IjKl",
    "AWS_SESSION_TOKEN=AbCd1234EfGh5678IjKl",
    "AZURE_CLIENT_SECRET=AbCd1234EfGh5678IjKl",
    "DATABASE_MASTER_PASSWORD=AbCd1234EfGh5678IjKl",
    "X-Api-Key: AbCd1234EfGh5678IjKlMn",
])
def test_multi_segment_credential_names_are_refused(secret):
    """`_` is a word character, so `\\b` never falls between the segments of
    AWS_SECRET_ACCESS_KEY. A single optional prefix segment caught HF_TOKEN and
    missed every three-segment cloud credential name there is."""
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"the deploy uses {secret}", known_secrets=[])


@pytest.mark.parametrize("text", [
    "the fixture uses password=aaaaaaaaaaaaaaaaaaaaaaaa as a placeholder",
    "set password=ResetYourPasswordNowPlease in the seed data",
    "the cache key is derived from the request path",
    "read the token from HF_TOKEN at runtime, never inline",
    "password=please-change-me-before-deploying",
])
def test_broadening_the_name_side_did_not_start_refusing_prose(text):
    """A guard people route around is worse than none: an assignment only
    counts when the VALUE looks like key material (20+ chars, letter+digit)."""
    taskguard.check_task(text, known_secrets=[])


@pytest.mark.parametrize("text", [
    "the cache key: 550e8400-e29b-41d4-a716-446655440000 collides",
    "commit key: 9fceb02d0ae598e95dc970b74767f19372d61af8 broke it",
    "object token: 5f2b8c1d4e6a7b9c0d1e2f3a4b5c6d7e broke the parser",
    "digest key: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934c"
    "a495991b7852b855 differs",
])
def test_a_uuid_or_a_hash_is_not_a_credential(text):
    """Both matched the assignment shape after `key:`. A review task that
    quotes the commit it is about must not be refused for quoting it."""
    taskguard.check_task(text, known_secrets=[])


@pytest.mark.parametrize("secret", [
    # Exactly sha1-shaped, but after a NAMED credential variable, so the
    # hash exemption must not apply: excusing this let a real 40-hex key
    # through for being the same shape as a commit.
    "API_KEY=0123456789abcdef0123456789abcdef01234567",
    "SECRET_KEY=e3b0c44298fc1c149afbf4c8996fb92427ae41e4"
    "649b934ca495991b7852b855",                        # sha256-shaped
    "API_KEY=A1B2C3D4E5F60718293A4B5C6D7E8F90",       # 32 chars, UPPER hex
    "api_key=0123456789abcdef0123456789abcdef01",      # 33 chars, not a hash
    "NGC_API_KEY=nvapi7f3a9b2c4d5e6f708192a3b4c5d6e7f8091a2b3",
    "GCP_ACCESS_TOKEN=ya29.a0AfB1eXAMPLEtoken1234567890abcdef",
])
def test_an_all_hex_key_is_not_mistaken_for_a_hash(secret):
    """The hash exemption above was written `[0-9a-f]{32,}` under `(?i)`.

    Case-insensitively that covers `[0-9A-F]` too and any length past 32, so it
    excused exactly the shape most API keys have — the exemption meant to stop
    false positives became a hole. Real hashes have exact lengths and are
    lowercase by convention.
    """
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"authenticate with {secret}", known_secrets=[])


@pytest.mark.parametrize("marker_line", [
    "- <!--PILOT_FILL a bullet -->",
    "* <!--PILOT_FILL a bullet -->",
    "> <!--PILOT_FILL a quote -->",
    "  - <!--PILOT_FILL nested -->",
])
def test_placeholder_in_a_list_or_quote_is_refused(marker_line):
    """Line anchoring allowed only whitespace, so a placeholder inside a bullet
    or blockquote — exactly where a hand-edited template puts one — slipped by."""
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(f"# Objective\n\n{marker_line}\n", known_secrets=[])


def test_a_long_dummy_value_is_not_mistaken_for_a_credential():
    """The assignment shape fired on any 20+ char run, so a fixture like
    `password=xxxxxxxxxxxxxxxxxxxxxx` was refused as a secret."""
    taskguard.check_task(
        "the fixture uses password=aaaaaaaaaaaaaaaaaaaaaaaa as a placeholder",
        known_secrets=[])


def test_temp_task_survives_no_exit_path(tmp_path, monkeypatch):
    """The unlink lived in a `finally` entered only after the child ran, so an
    interrupt or a failed Popen leaked a 0600 file holding the task."""
    from pilot_workers.cli import dispatch as dispatch_mod
    import glob
    import tempfile as tf

    before = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))

    def _boom(*_args, **_kwargs):
        raise OSError("cannot start runner")

    monkeypatch.setattr(dispatch_mod.subprocess, "Popen", _boom)
    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task", "inline text",
    ])
    dispatch_mod.run_dispatch(args)

    after = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))
    assert after == before, f"leaked on the Popen-failure path: {after - before}"


def test_temp_task_removed_on_keyboard_interrupt(tmp_path, monkeypatch):
    from pilot_workers.cli import dispatch as dispatch_mod
    import glob
    import tempfile as tf

    before = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))

    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(dispatch_mod.subprocess, "Popen", _interrupt)
    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task", "inline text",
    ])
    with pytest.raises(KeyboardInterrupt):
        dispatch_mod.run_dispatch(args)

    after = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))
    assert after == before, f"leaked on the interrupt path: {after - before}"


def test_temp_task_cleanup_is_registered_before_the_write(tmp_path, monkeypatch):
    """A disk-full during the write leaked the file: the cleanup callback was
    registered only after chmod and write had both succeeded."""
    from pilot_workers.cli import dispatch as dispatch_mod
    import glob
    import tempfile as tf

    before = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))

    real_chmod = dispatch_mod.os.chmod

    def _boom(path, mode, *a, **k):
        if ".pilot-task." in str(path):
            raise OSError(28, "No space left on device")
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(dispatch_mod.os, "chmod", _boom)
    args = dispatch_mod.parse_args([
        "--provider", "glm", "--mode", "review",
        "--workdir", str(tmp_path), "--task", "inline text",
    ])
    with pytest.raises(OSError):
        dispatch_mod.run_dispatch(args)

    after = set(glob.glob(str(Path(tf.gettempdir()) / ".pilot-task.*")))
    assert after == before, f"leaked when the write failed: {after - before}"


def test_a_configured_key_exactly_at_the_minimum_length_is_refused():
    """The boundary: `>= MIN_SECRET_LENGTH` vs `>`. Existing cases use 3 chars
    (ignored) and 24 (refused) — neither would notice the comparison flipping."""
    secret = "a" * taskguard.MIN_SECRET_LENGTH
    with pytest.raises(RuntimeError, match="credential"):
        taskguard.check_task(f"use {secret} here", known_secrets=[secret])


def test_a_configured_key_one_char_below_the_minimum_is_ignored():
    secret = "a" * (taskguard.MIN_SECRET_LENGTH - 1)
    taskguard.check_task(f"use {secret} here", known_secrets=[secret])


def test_a_multi_line_unfilled_placeholder_is_refused():
    """`_PLACEHOLDER` carries re.DOTALL for exactly this shape, and every other
    fixture is single-line — so the flag could be dropped unnoticed."""
    task = ("# Objective\n\n"
            "<!--PILOT_FILL what to achieve\n"
            "   across several lines\n"
            "   like a hand-edited template -->\n")
    with pytest.raises(RuntimeError, match="unfilled"):
        taskguard.check_task(task, known_secrets=[])
