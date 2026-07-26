"""Offline tests for credential setup folded into the provider install form.

The API key belongs to the PROVIDER, not the host: configured once, usable by
every host. It is prompted from ``install <provider> on <host> --global-key``
only because that is where the user's attention already is.

Nothing here touches a real key or a real home directory.
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest

from pilot_workers import credentials, providers, runtime
from pilot_workers.cli import install as install_mod
from pilot_workers.cli import main as main_mod
from pilot_workers.runners import get_runner


FAKE_KEY = "test-key-not-a-real-secret"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return {"home": tmp_path / "home", "target": tmp_path / "target"}


def _auth_path(provider_key: str):
    provider = providers.PROVIDERS[provider_key]
    return get_runner(provider.runner).credential_path(provider)


def _prompt(monkeypatch, value: str):
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: value)


# ----------------------------------------------------------------------
# the credentials subcommand is gone
# ----------------------------------------------------------------------


def test_credentials_subcommand_is_removed(capsys):
    assert main_mod.main(["credentials", "glm"]) == 2
    assert "unknown subcommand" in capsys.readouterr().err


def test_main_usage_no_longer_lists_credentials(capsys):
    """Check the SUBCOMMAND column, not the whole page: a bare substring test
    also fires on prose that merely mentions credentials."""
    main_mod.main([])
    listed = [line.split()[0] for line in capsys.readouterr().out.splitlines()
              if line.startswith("  ") and line.strip() and not line.startswith("   ")]
    assert "credentials" not in listed


# ----------------------------------------------------------------------
# --global-key writes the provider's key
# ----------------------------------------------------------------------


def test_global_key_writes_the_credential(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    rc = install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    assert rc == 0
    payload = json.loads(_auth_path("glm").read_text(encoding="utf-8"))
    assert payload[providers.PROVIDERS["glm"].provider_id]["key"] == FAKE_KEY


def test_credential_file_is_0600(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    assert stat.S_IMODE(_auth_path("glm").stat().st_mode) == 0o600


def test_global_key_also_records_the_config(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "for", "code", "--global-key",
         "--target", str(isolated["target"])])
    manifest = json.loads(
        (isolated["home"] / "install-manifest.json").read_text(encoding="utf-8"))
    entry = manifest["installs"]["claude"]
    assert entry["providers"] == ["glm"]
    assert entry["modes"] == {"code": "glm"}


def test_key_is_never_echoed(isolated, monkeypatch, capsys):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err


def test_empty_key_fails_and_records_nothing(isolated, monkeypatch, capsys):
    """The key is prompted before config is written, so a refusal changes nothing."""
    _prompt(monkeypatch, "   ")
    rc = install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    assert rc != 0
    assert "empty" in capsys.readouterr().err.lower()
    assert not (isolated["home"] / "install-manifest.json").exists()
    assert not _auth_path("glm").exists()


def test_global_key_is_shared_across_hosts(isolated, monkeypatch):
    """The key belongs to the provider: configuring it once serves every host."""
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    first = _auth_path("glm").read_text(encoding="utf-8")

    install_mod.main(["glm", "on", "codex", "--target", str(isolated["target"])])

    assert _auth_path("glm").read_text(encoding="utf-8") == first


# ----------------------------------------------------------------------
# without --global-key: remind, do not fail
# ----------------------------------------------------------------------


def test_missing_key_prints_a_reminder(isolated, capsys):
    rc = install_mod.main(
        ["glm", "on", "claude", "--target", str(isolated["target"])])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--global-key" in out


def test_reminder_does_not_block_the_config(isolated):
    install_mod.main(["glm", "on", "claude", "--target", str(isolated["target"])])
    manifest = json.loads(
        (isolated["home"] / "install-manifest.json").read_text(encoding="utf-8"))
    assert manifest["installs"]["claude"]["providers"] == ["glm"]


def test_no_reminder_once_the_key_exists(isolated, monkeypatch, capsys):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    capsys.readouterr()

    install_mod.main(["glm", "on", "codex", "--target", str(isolated["target"])])

    assert "--global-key" not in capsys.readouterr().out


def test_global_key_rejected_without_a_provider(isolated, capsys):
    """--global-key names no provider on its own, so the host form must reject it."""
    assert install_mod.main(
        ["claude", "--global-key", "--target", str(isolated["target"])]) == 2
    assert "usage:" in capsys.readouterr().err


# ----------------------------------------------------------------------
# error messages point at the surviving command
#
# These were source greps: the literal `--global-key` appearing ANYWHERE in
# the module satisfied them, docstrings included, so dropping it from the
# user-visible string kept them green. They are covered behaviourally by
# test_runtime_missing_credential_message_is_actionable and
# test_fanout_missing_credential_message_is_actionable below, which read the
# message the user actually gets.
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# cross-model review follow-ups
# ----------------------------------------------------------------------


def test_no_tty_fails_cleanly_without_a_traceback(isolated, monkeypatch, capsys):
    """getpass raises EOFError with no stdin; that must not escape as a crash."""
    def _eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("getpass.getpass", _eof)
    rc = install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
    assert not (isolated["home"] / "install-manifest.json").exists()


def test_interrupted_prompt_fails_cleanly(isolated, monkeypatch, capsys):
    """Ctrl-C at the prompt should not leave a half-written state either."""
    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("getpass.getpass", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        install_mod.main(
            ["glm", "on", "claude", "--global-key",
             "--target", str(isolated["target"])])
    assert not (isolated["home"] / "install-manifest.json").exists()
    assert not _auth_path("glm").exists()


def test_credentials_module_has_no_orphan_cli():
    """The subcommand is gone; a main() nobody routes to would rot."""
    from pilot_workers import credentials
    assert not hasattr(credentials, "main")


# ----------------------------------------------------------------------
# review LOW follow-ups
# ----------------------------------------------------------------------


def test_configure_rejects_an_unknown_provider_without_prompting(monkeypatch):
    """LOW-3: configure() opened with PROVIDERS[key], a KeyError its callers
    do not catch. It must raise RuntimeError instead, and must not prompt for a
    key it is about to reject — otherwise a provider YAML deleted mid-flight
    leaves an orphaned credential file.
    """
    from pilot_workers import credentials

    calls = []
    monkeypatch.setattr(
        "getpass.getpass", lambda *a, **k: calls.append(1) or FAKE_KEY)

    with pytest.raises(RuntimeError, match="unknown provider"):
        credentials.configure("no-such-provider")
    assert not calls, "must not prompt for a provider it is rejecting"


def test_stale_temp_credential_files_are_swept(isolated, monkeypatch):
    """LOW-2: a hard crash can strand a .auth.*.tmp holding a key.

    A finally-block cannot help there, so the next configure sweeps them.
    """
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    auth_dir = _auth_path("glm").parent
    stale = auth_dir / ".auth.leftover.tmp"
    stale.write_text("pretend-this-holds-a-key", encoding="utf-8")
    abandoned = time.time() - credentials.STALE_TMP_GRACE_SECONDS - 60
    os.utime(stale, (abandoned, abandoned))

    install_mod.main(
        ["glm", "on", "codex", "--global-key", "--target", str(isolated["target"])])

    assert not stale.exists()


def test_a_concurrent_writers_live_temp_file_is_not_swept(isolated, monkeypatch):
    """The sweep matches by name, so a second concurrent configure() of the
    same provider had its in-progress tmp deleted — and its os.replace then
    failed with FileNotFoundError for no reason at all."""
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.main(
        ["glm", "on", "claude", "--global-key", "--target", str(isolated["target"])])
    auth_dir = _auth_path("glm").parent
    live = auth_dir / ".auth.inflight.tmp"
    live.write_text("another writer is mid-write here", encoding="utf-8")

    credentials.configure("glm")

    assert live.exists(), "swept a temp file a concurrent writer still owns"


def test_runtime_missing_credential_message_is_actionable(isolated):
    """LOW-6: assert the message a user actually sees, not the source text."""
    from pilot_workers import runtime
    from pilot_workers.runners import get_runner

    provider = providers.PROVIDERS["glm"]
    with pytest.raises(RuntimeError) as excinfo:
        runtime.credential_key(provider, get_runner(provider.runner))
    message = str(excinfo.value)
    assert "credential missing for glm" in message
    assert "--global-key" in message


@pytest.mark.parametrize("payload", ['"just-a-string"', "[1, 2, 3]", "42"])
def test_a_non_object_credential_file_fails_with_a_clean_error(isolated, payload):
    """Valid JSON of the wrong shape reached the runner's ``.get`` directly.

    That raised AttributeError from inside parse_credential — outside every
    caller's catch list, so a malformed credential file crashed the dispatch
    with a traceback instead of naming the file.
    """
    from pilot_workers import runtime
    from pilot_workers.runners import get_runner

    provider = providers.PROVIDERS["glm"]
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(RuntimeError) as excinfo:
        runtime.credential_key(provider, get_runner(provider.runner))
    assert "not a JSON object" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_a_malformed_credential_never_crashes_the_task_guard(isolated):
    """`_configured_secrets` opens every provider's key file on every dispatch;
    one bad file must not be the reason a dispatch dies."""
    from pilot_workers.cli.run import _configured_secrets

    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('"not-an-object"', encoding="utf-8")
    path.chmod(0o600)

    assert _configured_secrets() == []


def test_fanout_missing_credential_message_is_actionable(isolated, tmp_path):
    """LOW-6: drive fanout's preflight and read its real error."""
    from pilot_workers.cli import fanout

    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    jobs = [fanout.Job(provider="glm", mode="review", task_file=str(task))]

    with pytest.raises(Exception) as excinfo:
        fanout._credential_preflight(jobs)
    message = str(excinfo.value)
    assert "credential missing for glm" in message
    assert "--global-key" in message


def test_key_warning_survives_an_unreadable_credential_dir(isolated, monkeypatch):
    """LOW-5: the warning helper's except path had zero coverage.

    A divergence warning must never be the thing that breaks the command.
    """
    from pilot_workers import credentials as creds

    def _boom(_key):
        raise OSError("cannot stat")

    monkeypatch.setattr(creds, "credential_status", _boom)
    assert install_mod.main(
        ["glm", "on", "claude", "--target", str(isolated["target"])]) == 0


# ----------------------------------------------------------------------
# An insecurely-stored key must still be REDACTABLE.
#
# `credential_key` refuses a credential file wider than 0600 — right for the
# dispatch path, wrong for `configured_secrets`, which exists only to build the
# redaction list. Catching that refusal dropped the key from the list, so a
# worker for provider A that echoed provider B's key reached the planner
# unredacted precisely when B's file was insecure. The existing redaction test
# monkeypatches `configured_secrets`, so the suite could not see this.
# ----------------------------------------------------------------------

def test_an_insecurely_stored_key_is_still_redacted(isolated):
    from pilot_workers.cli.run import _configured_secrets

    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"glm-worker": {"type": "api", "key": "sk-insecure-1234567890abcd"}}),
        encoding="utf-8")
    path.chmod(0o644)               # group/other readable: exactly the risk case

    secrets = _configured_secrets()
    assert "sk-insecure-1234567890abcd" in secrets, (
        "an insecure key was dropped from the redaction list")
    assert runtime.redact_secrets(
        "leaked sk-insecure-1234567890abcd here", secrets) == "leaked [REDACTED] here"


def test_the_dispatch_path_still_refuses_an_insecure_credential(isolated):
    """Reverse assertion: making the key redactable must NOT make it usable."""
    from pilot_workers.runners import get_runner

    provider = providers.PROVIDERS["glm"]
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"glm-worker": {"type": "api", "key": "sk-insecure-1234567890abcd"}}),
        encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(RuntimeError, match="not private"):
        runtime.credential_key(provider, get_runner(provider.runner))


@pytest.mark.parametrize("argv", [
    ["runner", "opencode", "--global-key"],
    ["runner", "opencode", "--global-key", "--target", "/tmp/x"],
])
def test_global_key_is_refused_on_the_runner_form(isolated, capsys, argv):
    """The runner branch returned before the flag check, so the flag was consumed,
    did nothing and said nothing. Reported independently by ds and kimi."""
    assert install_mod.main(argv) == 2
    assert "no meaning for 'runner'" in capsys.readouterr().err


def test_global_key_still_works_on_the_provider_form(isolated, monkeypatch, capsys):
    """Reverse assertion: the new check must not reach the form the flag is for."""
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "fake-key-value-xyz")
    assert install_mod.main(
        ["glm", "on", "claude", "--global-key",
         "--target", str(isolated["target"])]) == 0


def test_a_credential_removed_mid_read_gives_a_clean_error(isolated, monkeypatch):
    """Both readers stat and read the file; a credential removed between the two
    must produce this layer's own message, not a bare OSError.

    Neither had a test: every caller monkeypatches the function, so glm found the
    gap by reverting the fix and watching the suite stay green.
    """
    from pilot_workers.runners import get_runner

    provider = providers.PROVIDERS["glm"]
    runner = get_runner(provider.runner)
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({provider.provider_id: {"type": "api", "key": "k" * 20}}),
        encoding="utf-8")
    path.chmod(0o600)

    real_read = type(path).read_text

    def vanishing_read(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", vanishing_read)

    with pytest.raises(RuntimeError, match="cannot read credential"):
        runtime.credential_key(provider, runner)
    # And the metadata reader answers "not configured" instead of raising.
    meta = runtime.credential_metadata(provider, runner)
    assert meta["configured"] is False


def test_a_credential_that_vanishes_before_the_stat_is_also_clean(
        isolated, monkeypatch):
    """The other order: gone before the mode check rather than before the read.

    Patched through monkeypatch, not by assigning to the class. The first version
    set `type(path).stat` directly and restored it in a `finally`, so a failure
    anywhere inside would leak a broken `Path.stat` into every later test — kimi
    flagged it, and it is a suspect for a red baseline I could not reproduce.
    monkeypatch restores even when the test raises.
    """
    from pilot_workers.runners import get_runner

    provider = providers.PROVIDERS["glm"]
    runner = get_runner(provider.runner)
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)

    real_stat = type(path).stat

    def vanishing_stat(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError(2, "gone", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "stat", vanishing_stat)
    with pytest.raises(RuntimeError, match="cannot read credential"):
        runtime.credential_key(provider, runner)
