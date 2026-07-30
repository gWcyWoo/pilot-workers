"""Guard: no test may reach the developer's real config directories.

Credential and runner paths derive from ``$CODEX_HOME`` / ``Path.home()``.
``tests/conftest.py`` redirects the home directory for every test; these
tests fail loudly if that stops working.
"""

from __future__ import annotations

import os
from pathlib import Path


def test_home_env_is_redirected():
    assert "pytest" in os.environ["HOME"]


def test_path_home_is_redirected():
    """Path.home() reads the password database on some platforms, not $HOME."""
    assert "pytest" in str(Path.home())


def test_codex_home_is_redirected():
    assert "pytest" in os.environ["CODEX_HOME"]


def test_pilot_home_lands_inside_the_sandbox():
    from pilot_workers import providers
    assert "pytest" in str(providers.pilot_home())


def test_home_based_paths_land_inside_the_sandbox():
    assert "pytest" in str(Path.home() / ".codex")


def test_a_dispatch_child_imports_the_same_package_as_the_parent():
    """A child spawned by dispatch must not resolve to a different install.

    `dispatch` runs `sys.executable -m pilot_workers.cli.run`. Under pytest the
    parent imports from `src/` (pythonpath), while a bare child resolves
    site-packages — which can be a stale snapshot. Every test that goes through
    run_dispatch would then be validating code that is not under review.
    """
    import subprocess
    import sys

    import pilot_workers
    from pilot_workers.cli import dispatch as dispatch_mod

    cmd = dispatch_mod._build_runner_command(
        "glm", "review", "/tmp", None, "/tmp/t.md", None, False, 60, 60)
    env = dispatch_mod._child_environment()

    proc = subprocess.run(
        [cmd[0], "-c", "import pilot_workers, pathlib;"
                       "print(pathlib.Path(pilot_workers.__file__).parent)"],
        capture_output=True, text=True, env=env, check=True,
    )
    child_pkg = proc.stdout.strip()
    parent_pkg = str(Path(pilot_workers.__file__).parent)
    assert child_pkg == parent_pkg, (
        f"child imports {child_pkg}, parent imports {parent_pkg}")


def test_the_child_cannot_be_hijacked_by_its_working_directory():
    """`python -m` puts cwd at sys.path[0], AHEAD of PYTHONPATH.

    The child USED TO run with cwd in a shared temp dir, so a `pilot_workers/`
    (or even `json.py`) dropped there by any local process would have been
    imported in preference to the real package — and the pinned PYTHONPATH could
    not have stopped it. It now runs in a directory this tool owns.
    """
    import subprocess

    from pilot_workers.cli import dispatch as dispatch_mod

    cmd = dispatch_mod._build_runner_command(
        "glm", "review", "/tmp", None, "/tmp/t.md", None, False, 60, 60)
    env = dispatch_mod._child_environment()

    proc = subprocess.run(
        [cmd[0], "-c", "import sys; print(sys.path[0])"],
        capture_output=True, text=True, env=env, check=True,
        cwd=dispatch_mod._child_cwd(),
    )
    first = proc.stdout.strip()
    assert first not in ("", "."), "cwd is first on sys.path"
    assert "/tmp" != first, "the child still runs from a world-writable cwd"


def test_the_runner_version_cache_starts_empty_in_every_test():
    """Proven by two tests in a row: a module-level dict outlives a test, so a
    cached answer from an earlier one could satisfy a later probe and let a
    broken probe pass."""
    from pilot_workers.runners import opencode_runner

    assert opencode_runner._VERSION_CACHE == {}
    opencode_runner._VERSION_CACHE[Path("/fake/binary")] = (0, 0, "9.9.9")


def test_the_version_cache_was_cleared_after_the_previous_test():
    from pilot_workers.runners import opencode_runner

    assert opencode_runner._VERSION_CACHE == {}, (
        "state leaked from the previous test")


def test_pilot_home_derives_from_the_redirected_codex_home():
    """The on-disk half of the same cache lives under pilot_home()."""
    from pilot_workers import providers

    assert "pytest" in str(providers.pilot_home())
