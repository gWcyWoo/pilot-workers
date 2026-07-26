"""Global test isolation.

Some commands deploy to a host's DEFAULT directory (``~/.claude``,
``$CODEX_HOME``) when no ``--target`` is given. A test that forgets to pass one
would otherwise write into the developer's real config, so the home directory is
redirected for every test rather than per-fixture — forgetting is silent, and the
damage lands outside the repo where nothing notices.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clear_process_wide_caches():
    """Module-level caches outlive a test and couple it to the next one.

    The runner keeps an in-process ``--version`` cache keyed by binary path.
    Its on-disk half is already per-test (``CODEX_HOME`` is redirected below, and
    ``pilot_home()`` derives from it), but the dict is not: one test's cached
    answer could satisfy another test's probe, so a broken probe would pass.
    """
    from pilot_workers.runners import opencode_runner

    opencode_runner.clear_version_cache()
    yield
    opencode_runner.clear_version_cache()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    # providers.pilot_home() and install_host() both reach for Path.home(),
    # which reads the password database rather than $HOME on some platforms.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home
