"""RED tests for the D6 opencode ``--version`` check cache (design-v0.5.0 D6).

Pinned contract:

- ``opencode_runner`` owns a module-level ``_VERSION_CACHE`` keyed on
  ``(path, mtime_ns, size)``: ``dict[Path, tuple[int, int, str]]`` mapping
  binary path -> (mtime_ns, size, version_string).
- ``resolve_binary()`` runs the ``--version`` subprocess on first sight and
  populates the cache; an UNCHANGED (mtime_ns, size) is served from the
  cache with no subprocess; a touched file (changed mtime_ns) re-runs the
  check; a version mismatch still raises and is never cached.
- ``clear_version_cache()`` empties the cache.
- ``cli.install._install_runner`` calls ``clear_version_cache()`` after a
  successful install (and not on a failed one).
- ``cli.status`` shares the same seam: two consecutive ``_collect()``-level
  runner checks spawn the version subprocess only once.

Conventions follow tests/test_install.py: ``subprocess.run`` is monkeypatched
with a counting fake; the real install script is never executed.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from pilot_workers.cli import install as install_mod
from pilot_workers.cli import status as status_mod
from pilot_workers.runners import get_runner
from pilot_workers.runners import opencode_runner
from pilot_workers.runners.opencode_runner import PINNED_OPENCODE_VERSION


@pytest.fixture(autouse=True)
def _isolate_version_cache():
    """Keep the module-level cache from leaking between tests (no-op until
    the cache API exists)."""
    cache = getattr(opencode_runner, "_VERSION_CACHE", None)
    if cache is not None:
        cache.clear()
    yield
    if cache is not None:
        cache.clear()


@pytest.fixture
def fake_binary(tmp_path):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def runner(fake_binary, monkeypatch):
    instance = get_runner("opencode")
    monkeypatch.setattr(instance, "binary_path", lambda: fake_binary)
    return instance


def _ok_run(calls):
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=PINNED_OPENCODE_VERSION, stderr="",
        )
    return fake_run


@pytest.fixture
def version_calls(monkeypatch):
    """Counting ``subprocess.run`` fake that answers the pinned version."""
    calls: list = []
    monkeypatch.setattr("subprocess.run", _ok_run(calls))
    return calls


# ----------------------------------------------------------------------
# opencode_runner: _VERSION_CACHE + resolve_binary caching
# ----------------------------------------------------------------------


def test_version_cache_entry_shape(runner, fake_binary, version_calls):
    runner.resolve_binary()
    cache = opencode_runner._VERSION_CACHE
    assert isinstance(cache, dict)
    # The key is the binary path as resolved by resolve_binary (allow a
    # resolved form for symlinked tmp dirs).
    key = fake_binary if fake_binary in cache else fake_binary.resolve()
    st = fake_binary.stat()
    assert cache.get(key) == (st.st_mtime_ns, st.st_size, PINNED_OPENCODE_VERSION)


def test_resolve_binary_caches_unchanged_binary(runner, fake_binary, version_calls):
    assert runner.resolve_binary() == fake_binary
    assert runner.resolve_binary() == fake_binary
    assert len(version_calls) == 1, (
        "second resolve_binary with unchanged (mtime_ns, size) must not "
        "spawn the --version subprocess"
    )


def test_resolve_binary_reruns_when_mtime_changes(runner, fake_binary, version_calls):
    runner.resolve_binary()
    assert len(version_calls) == 1
    st = fake_binary.stat()
    os.utime(fake_binary, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    runner.resolve_binary()
    assert len(version_calls) == 2, (
        "touching the binary (changed mtime_ns) must re-run the version check"
    )


def test_version_mismatch_still_raises_and_is_not_cached(
    runner, monkeypatch,
):
    calls: list = []

    def wrong_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="0.0.0", stderr="")

    monkeypatch.setattr("subprocess.run", wrong_run)
    with pytest.raises(RuntimeError, match="expected OpenCode"):
        runner.resolve_binary()

    # A failed check must not be cached: once the binary reports the pinned
    # version, resolve succeeds and the subprocess ran again.
    monkeypatch.setattr("subprocess.run", _ok_run(calls))
    runner.resolve_binary()
    assert len(calls) == 2


def test_clear_version_cache_empties_cache(runner, fake_binary, version_calls):
    runner.resolve_binary()
    assert len(version_calls) == 1
    assert opencode_runner._VERSION_CACHE, "cache should be populated"

    opencode_runner.clear_version_cache()
    assert opencode_runner._VERSION_CACHE == {}

    runner.resolve_binary()
    assert len(version_calls) == 2, (
        "after clear_version_cache() the next resolve must re-run the check"
    )


# ----------------------------------------------------------------------
# cli/install: _install_runner clears the cache after a successful install
# ----------------------------------------------------------------------


def _patch_clear_version_cache(monkeypatch):
    """Record clear_version_cache() invocations regardless of whether the
    GREEN implementation imports it at install-module top level or from
    opencode_runner at call time."""
    cleared: list = []
    recorder = lambda: cleared.append(True)  # noqa: E731
    monkeypatch.setattr(
        opencode_runner, "clear_version_cache", recorder, raising=False,
    )
    monkeypatch.setattr(
        install_mod, "clear_version_cache", recorder, raising=False,
    )
    return cleared


def test_install_runner_clears_version_cache_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    cleared = _patch_clear_version_cache(monkeypatch)

    def mock_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", mock_run)
    rc = install_mod._install_runner("opencode")
    assert rc == 0
    assert cleared, (
        "_install_runner must call clear_version_cache() after a "
        "successful install"
    )


def test_install_runner_keeps_cache_on_failed_install(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path))
    cleared = _patch_clear_version_cache(monkeypatch)

    def mock_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr("subprocess.run", mock_run)
    rc = install_mod._install_runner("opencode")
    assert rc == 1
    assert not cleared, (
        "clear_version_cache() is only for a successful install"
    )


# ----------------------------------------------------------------------
# cli/status: two _collect()-level runner checks share the cached seam
# ----------------------------------------------------------------------


def test_status_collect_spawns_version_check_once(monkeypatch, tmp_path):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    # Plant an executable at the runner's expected binary path so the status
    # runner check actually performs a version probe.
    binary = get_runner("opencode").binary_path()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    calls: list = []
    monkeypatch.setattr("subprocess.run", _ok_run(calls))

    status_mod._collect()
    status_mod._collect()

    version_probes = [c for c in calls if "--version" in [str(a) for a in c]]
    assert len(version_probes) == 1, (
        "two consecutive status runner checks must share the D6 version "
        "cache and spawn the --version subprocess only once"
    )
