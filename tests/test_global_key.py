"""Offline tests for credential setup via pw9 key <provider>.

The API key belongs to the PROVIDER, not any host: configured once, usable
everywhere. Nothing here touches a real key or a real home directory.
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
# pw9 key <provider>
# ----------------------------------------------------------------------


def test_key_writes_the_credential(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    rc = install_mod.key_main(["glm"])
    assert rc == 0
    payload = json.loads(_auth_path("glm").read_text(encoding="utf-8"))
    assert payload[providers.PROVIDERS["glm"].provider_id]["key"] == FAKE_KEY


def test_credential_file_is_0600(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.key_main(["glm"])
    assert stat.S_IMODE(_auth_path("glm").stat().st_mode) == 0o600


def test_key_is_never_echoed(isolated, monkeypatch, capsys):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.key_main(["glm"])
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out
    assert FAKE_KEY not in captured.err


def test_empty_key_fails(isolated, monkeypatch, capsys):
    _prompt(monkeypatch, "   ")
    rc = install_mod.key_main(["glm"])
    assert rc != 0
    assert not _auth_path("glm").exists()


def test_unknown_provider_returns_2(isolated, capsys):
    rc = install_mod.key_main(["bogus"])
    assert rc == 2
    assert "unknown provider" in capsys.readouterr().err


def test_credentials_subcommand_is_removed(capsys):
    assert main_mod.main(["credentials", "glm"]) == 2
    assert "unknown subcommand" in capsys.readouterr().err


def test_credentials_module_has_no_orphan_cli():
    assert not hasattr(credentials, "main")


# ----------------------------------------------------------------------
# uninstall key
# ----------------------------------------------------------------------


def test_uninstall_key(isolated, monkeypatch, capsys):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.key_main(["glm"])
    assert _auth_path("glm").is_file()

    assert install_mod.uninstall_main(["key", "glm"]) == 0
    assert not _auth_path("glm").exists()


def test_removing_absent_key_is_not_an_error(isolated, capsys):
    assert install_mod.uninstall_main(["key", "glm"]) == 0
    assert "no" in capsys.readouterr().out.lower()


# ----------------------------------------------------------------------
# error messages
# ----------------------------------------------------------------------


def test_no_tty_fails_cleanly(isolated, monkeypatch, capsys):
    def _eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("getpass.getpass", _eof)
    rc = install_mod.key_main(["glm"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_runtime_missing_credential_message_is_actionable(isolated):
    provider = providers.PROVIDERS["glm"]
    with pytest.raises(RuntimeError) as excinfo:
        runtime.credential_key(provider, get_runner(provider.runner))
    message = str(excinfo.value)
    assert "credential missing for glm" in message
    assert "pw9 key glm" in message


def test_configure_rejects_an_unknown_provider_without_prompting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "getpass.getpass", lambda *a, **k: calls.append(1) or FAKE_KEY)
    with pytest.raises(RuntimeError, match="unknown provider"):
        credentials.configure("no-such-provider")
    assert not calls


def test_stale_temp_credential_files_are_swept(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.key_main(["glm"])
    auth_dir = _auth_path("glm").parent
    stale = auth_dir / ".auth.leftover.tmp"
    stale.write_text("pretend-this-holds-a-key", encoding="utf-8")
    abandoned = time.time() - credentials.STALE_TMP_GRACE_SECONDS - 60
    os.utime(stale, (abandoned, abandoned))

    credentials.configure("glm")
    assert not stale.exists()


def test_a_concurrent_writers_live_temp_file_is_not_swept(isolated, monkeypatch):
    _prompt(monkeypatch, FAKE_KEY)
    install_mod.key_main(["glm"])
    auth_dir = _auth_path("glm").parent
    live = auth_dir / ".auth.inflight.tmp"
    live.write_text("another writer is mid-write here", encoding="utf-8")

    credentials.configure("glm")
    assert live.exists(), "swept a temp file a concurrent writer still owns"


# ----------------------------------------------------------------------
# insecure credential redaction
# ----------------------------------------------------------------------


def test_an_insecurely_stored_key_is_still_redacted(isolated):
    from pilot_workers.cli.run import _configured_secrets

    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"glm-worker": {"type": "api", "key": "sk-insecure-1234567890abcd"}}),
        encoding="utf-8")
    path.chmod(0o644)

    secrets = _configured_secrets()
    assert "sk-insecure-1234567890abcd" in secrets
    assert runtime.redact_secrets(
        "leaked sk-insecure-1234567890abcd here", secrets) == "leaked [REDACTED] here"


def test_the_dispatch_path_still_refuses_an_insecure_credential(isolated):
    provider = providers.PROVIDERS["glm"]
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"glm-worker": {"type": "api", "key": "sk-insecure-1234567890abcd"}}),
        encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(RuntimeError, match="not private"):
        runtime.credential_key(provider, get_runner(provider.runner))


@pytest.mark.parametrize("payload", ['"just-a-string"', "[1, 2, 3]", "42"])
def test_a_non_object_credential_file_fails_cleanly(isolated, payload):
    provider = providers.PROVIDERS["glm"]
    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(RuntimeError) as excinfo:
        runtime.credential_key(provider, get_runner(provider.runner))
    assert "not a JSON object" in str(excinfo.value)


def test_a_malformed_credential_never_crashes_the_task_guard(isolated):
    from pilot_workers.cli.run import _configured_secrets

    path = _auth_path("glm")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('"not-an-object"', encoding="utf-8")
    path.chmod(0o600)

    assert _configured_secrets() == []


def test_fanout_missing_credential_message_is_actionable(isolated, tmp_path):
    from pilot_workers.cli import fanout

    task = tmp_path / "t.md"
    task.write_text("do a thing", encoding="utf-8")
    jobs = [fanout.Job(provider="glm", mode="review", task_file=str(task))]

    with pytest.raises(Exception) as excinfo:
        fanout._credential_preflight(jobs)
    message = str(excinfo.value)
    assert "credential missing for glm" in message
    assert "pw9 key glm" in message


def test_a_credential_removed_mid_read_gives_a_clean_error(isolated, monkeypatch):
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
    meta = runtime.credential_metadata(provider, runner)
    assert meta["configured"] is False


# ----------------------------------------------------------------------
# oauth credentials: the engine writes them, pw9 only reads the token
# ----------------------------------------------------------------------


def test_an_oauth_credential_is_parsed_for_redaction(isolated):
    """An oauth provider's auth.json is written by the ENGINE, in its own
    shape. pw9 reads one thing out of it: the live bearer token, so the
    dispatch can keep it out of the transcript."""
    provider = providers.PROVIDERS["codex"]
    runner = get_runner(provider.runner)
    path = runner.credential_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"openai": {
        "type": "oauth",
        "access": "sk-oauth-access-token-value",
        "refresh": "refresh-token-value",
        "expires": 9999999999,
    }}), encoding="utf-8")
    path.chmod(0o600)

    assert runtime.credential_key(provider, runner) == "sk-oauth-access-token-value"


def test_an_oauth_access_token_is_redacted_from_output(isolated):
    from pilot_workers.cli.run import _configured_secrets

    provider = providers.PROVIDERS["codex"]
    path = get_runner(provider.runner).credential_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"openai": {
        "type": "oauth", "access": "sk-oauth-leaked-1234567890",
        "refresh": "r", "expires": 1,
    }}), encoding="utf-8")
    path.chmod(0o600)

    secrets = _configured_secrets()
    assert "sk-oauth-leaked-1234567890" in secrets
    assert runtime.redact_secrets(
        "token sk-oauth-leaked-1234567890 here", secrets) == "token [REDACTED] here"


def test_an_unknown_credential_type_is_still_refused(isolated):
    """Reverse assertion: widening to oauth must not accept anything."""
    provider = providers.PROVIDERS["codex"]
    runner = get_runner(provider.runner)
    path = runner.credential_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"openai": {"type": "bearer", "token": "x" * 20}}),
                    encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(RuntimeError, match="lacks API or oauth auth"):
        runtime.credential_key(provider, runner)


def test_pw9_key_on_an_oauth_provider_does_not_prompt(isolated, monkeypatch, capsys):
    """`pw9 key codex` must hand the flow to the engine, not read a pasted
    string: an OAuth grant cannot be typed in."""
    calls = []
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: calls.append(1) or "x")
    # No runner installed in the isolated home, so this exits early — the point
    # is that it took the delegation path instead of prompting.
    rc = install_mod.key_main(["codex"])
    assert not calls, "an oauth provider must never reach the getpass prompt"
    assert rc != 0
    assert "install runner" in capsys.readouterr().err
