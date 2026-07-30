"""Error messages must name what went wrong, not just print the grammar.

Every refusal that can land on a real operator is checked for content: the
token they typed, the value they meant, the form they should have used.
"""

from __future__ import annotations

import pytest

from pilot_workers.cli import install as install_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return {"home": tmp_path / "home", "target": tmp_path / "target"}


# ----------------------------------------------------------------------
# credentials
# ----------------------------------------------------------------------


def test_a_credential_can_be_removed(isolated, monkeypatch, capsys):
    from pilot_workers import providers
    from pilot_workers.runners import get_runner

    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "fake-key-value-xyz")
    assert install_mod.key_main(["glm"]) == 0

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
    from pilot_workers import providers
    assert "key" in providers.RESERVED_PROVIDER_KEYS


def test_usage_lists_the_credential_removal_command():
    from pilot_workers.cli import main as main_module
    assert "uninstall key <provider>" in main_module.USAGE


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------


def test_install_runner_unknown_name(isolated, capsys):
    assert install_mod.main(["runner", "bogus"]) == 2
    assert "unknown runner" in capsys.readouterr().err


def test_uninstall_runner_unknown_name(isolated, capsys):
    assert install_mod.uninstall_main(["runner", "bogus"]) == 2
    assert "unknown runner" in capsys.readouterr().err


def test_key_unknown_provider(isolated, capsys):
    assert install_mod.key_main(["bogus"]) == 2
    assert "unknown provider" in capsys.readouterr().err
