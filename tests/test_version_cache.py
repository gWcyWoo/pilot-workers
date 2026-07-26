"""The runner version probe must survive across processes.

Every dispatch is a fresh Python process, so a module-scoped cache never hits:
each dispatch paid a ~330ms Node subprocess to re-learn a version that had not
changed. The cache has to live on disk, keyed by what makes the answer stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pilot_workers.runners import opencode_runner as oc


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    oc.clear_version_cache()
    return tmp_path


@pytest.fixture
def fake_binary(isolated):
    binary = isolated / "opencode"
    binary.write_text("#!/bin/sh\necho 9.9.9\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_probe_returns_the_version(fake_binary):
    assert oc.probe_version(fake_binary) == "9.9.9"


def _fresh_process() -> None:
    """What a new dispatch process starts with: an empty in-memory dict and the
    on-disk cache still there. NOT ``clear_version_cache()`` — that is the
    explicit full invalidation ``install runner`` asks for, and it deletes the
    disk half too, which is the opposite of the situation under test.
    """
    oc._VERSION_CACHE.clear()


def test_a_second_process_does_not_reprobe(fake_binary, monkeypatch):
    """The whole point: a fresh process must be served from disk."""
    assert oc.probe_version(fake_binary) == "9.9.9"

    _fresh_process()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("re-probed a binary whose version was already known")

    monkeypatch.setattr(oc.subprocess, "run", _forbidden)
    assert oc.probe_version(fake_binary) == "9.9.9"


def test_a_changed_binary_is_reprobed(fake_binary):
    # _fresh_process, not clear_version_cache: the disk entry must still be
    # there, or the STAMP check this test exists for is never exercised.
    assert oc.probe_version(fake_binary) == "9.9.9"
    _fresh_process()

    fake_binary.write_text("#!/bin/sh\necho 1.2.3\n", encoding="utf-8")
    fake_binary.chmod(0o755)

    assert oc.probe_version(fake_binary) == "1.2.3", "stale version served"


def test_a_failed_probe_is_not_cached(isolated, monkeypatch):
    broken = isolated / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)

    assert oc.probe_version(broken) is None
    _fresh_process()

    calls = []
    real = oc.subprocess.run

    def _count(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(oc.subprocess, "run", _count)
    oc.probe_version(broken)
    assert calls, "a failed probe must not be remembered as an answer"


def test_an_unwritable_cache_does_not_break_the_probe(fake_binary, monkeypatch):
    """A cache is an optimisation; it must never be the reason a dispatch fails."""
    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(oc, "_write_version_cache", _boom)
    assert oc.probe_version(fake_binary) == "9.9.9"


def test_every_in_place_rewrite_goes_through_one_writer():
    """Four hand-rolled atomic writers had already drifted apart.

    They disagreed on chmod placement and one had no fsync at all, so a
    crash-safety fix reached whichever copy the author was looking at. The
    credential writer stays separate on purpose: it must also sweep abandoned
    key temp files, which no other caller should do.
    """
    import re
    from pathlib import Path

    from pilot_workers import runtime
    from pilot_workers.cli import dispatch as dispatch_mod
    from pilot_workers.cli import install as install_mod
    from pilot_workers.runners import opencode_runner

    # Walk the package rather than an enumerated list: a new module with its own
    # atomic replace would have slipped past a three-module check unnoticed.
    package = Path(runtime.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path.name in ("runtime.py", "credentials.py"):
            continue  # the shared writer itself, and the key writer that must
            # also sweep abandoned temp files (see its docstring).
        if "os.replace(" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(package)))
    assert not offenders, f"modules rolling their own atomic replace: {offenders}"

    assert callable(runtime.atomic_write_text)


def test_the_shared_writer_never_exposes_a_wide_file(tmp_path):
    """Mode applied before the rename: the final name is never briefly wider."""
    import stat

    from pilot_workers import runtime

    target = tmp_path / "nested" / "thing.json"
    runtime.atomic_write_text(target, "payload", mode=0o600)
    assert target.read_text(encoding="utf-8") == "payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("**/*.tmp"))


def test_the_shared_writer_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    from pilot_workers import runtime

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtime.os, "replace", boom)
    with pytest.raises(OSError):
        runtime.atomic_write_text(tmp_path / "thing.json", "payload")
    assert not list(tmp_path.glob("*.tmp"))


# ----------------------------------------------------------------------
# The probe must be bounded.
#
# `resolve_binary` runs on the dispatch path (cli/run.py) BEFORE the started
# event is printed and before run_process arms --timeout/--idle-timeout, and
# `cli/dispatch.py` waits on that child with a bare proc.wait(). An engine
# binary that never answers `--version` therefore hung the whole dispatch with
# no output and no deadline: proven by a stub that sleeps, which kept
# resolve_binary blocked past a 12s external timeout.
# ----------------------------------------------------------------------

@pytest.fixture
def hung_binary(isolated):
    """A binary that never answers --version."""
    binary = isolated / "hung"
    binary.write_text("#!/bin/sh\nexec sleep 600\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_a_hung_binary_does_not_block_the_probe_forever(hung_binary, monkeypatch):
    import time

    monkeypatch.setattr(oc, "VERSION_PROBE_TIMEOUT_S", 1)
    start = time.monotonic()
    assert oc.probe_version(hung_binary) is None
    assert time.monotonic() - start < 10, "the probe was not bounded"


def test_a_hung_binary_does_not_block_resolve_binary_forever(hung_binary, monkeypatch):
    import time

    monkeypatch.setattr(oc, "VERSION_PROBE_TIMEOUT_S", 1)
    runner = oc.OpenCodeRunner()
    monkeypatch.setattr(runner, "binary_path", lambda: hung_binary)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="did not answer"):
        runner.resolve_binary()
    assert time.monotonic() - start < 10, "resolve_binary was not bounded"


def test_a_timed_out_probe_is_not_cached(hung_binary, monkeypatch):
    """A binary that was merely slow once must be re-probed, not remembered as
    unknown forever."""
    monkeypatch.setattr(oc, "VERSION_PROBE_TIMEOUT_S", 1)
    oc.probe_version(hung_binary)
    assert hung_binary not in oc._VERSION_CACHE
    assert str(hung_binary) not in oc._read_version_cache()


def test_the_base_runner_default_probe_is_bounded(hung_binary, monkeypatch):
    """The seam every future runner inherits, not just OpenCode's override."""
    import time

    from pilot_workers.runners import base

    monkeypatch.setattr(base, "VERSION_PROBE_TIMEOUT_S", 1)

    class Bare(base.Runner):
        name = "bare"
        build_config = build_command = runner_environment = None
        format_task_input = parse_events = resolve_binary = None
        credential_path = credential_payload = parse_credential = None

    Bare.__abstractmethods__ = frozenset()
    start = time.monotonic()
    assert Bare().probe_version(hung_binary) is None
    assert time.monotonic() - start < 10, "the base default probe was not bounded"


def test_the_probe_does_not_inherit_an_open_parent_stdin(isolated):
    """A probe that reads stdin blocks on whatever the parent had open.

    The parent's stdin must be a pipe that never reaches EOF, and the write end
    has to stay open in THIS process. The first version of this test passed
    ``stdin=subprocess.PIPE`` and called ``communicate()`` with no input —
    which closes the child's stdin immediately, so the stub saw EOF and the
    test passed against the unfixed code too. Verified to discriminate: with
    the ``stdin=DEVNULL`` removed the probe blocks past the timeout below.
    """
    import os
    import subprocess
    import sys

    import pilot_workers

    binary = isolated / "reads-stdin"
    binary.write_text("#!/bin/sh\ncat > /dev/null\necho 9.9.9\n", encoding="utf-8")
    binary.chmod(0o755)

    src = str(Path(pilot_workers.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src,
           "PILOT_WORKERS_HOME": str(isolated / "home")}
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys;"
             "from pathlib import Path;"
             "from pilot_workers.runners import opencode_runner as oc;"
             "print(oc.probe_version(Path(sys.argv[1])))",
             str(binary)],
            stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        os.close(read_fd)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AssertionError(
                "the probe inherited the parent's open stdin and blocked on it")
    finally:
        os.close(write_fd)
    assert proc.returncode == 0, err
    assert out.strip() == "9.9.9", f"out={out!r} err={err!r}"


# ----------------------------------------------------------------------
# The disk cache has to serve the DISPATCH path, not just `status`.
#
# resolve_binary consulted only the in-memory dict — which is always empty on
# that path, because every dispatch is a fresh process. So the ~330ms Node
# startup this cache exists to remove was still paid on every single run, and a
# successful verify was never written to disk either. Exactly the defect this
# module's docstring describes, left live on the path that pays for it.
# ----------------------------------------------------------------------

@pytest.fixture
def pinned_binary(isolated):
    binary = isolated / "opencode-pinned"
    binary.write_text(f"#!/bin/sh\necho {oc.PINNED_OPENCODE_VERSION}\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _runner_for(binary, monkeypatch):
    runner = oc.OpenCodeRunner()
    monkeypatch.setattr(runner, "binary_path", lambda: binary)
    return runner


def test_resolve_binary_does_not_reprobe_in_a_fresh_process(pinned_binary, monkeypatch):
    runner = _runner_for(pinned_binary, monkeypatch)
    assert runner.resolve_binary() == pinned_binary

    _fresh_process()

    def forbidden(*args, **kwargs):
        raise AssertionError("resolve_binary re-probed instead of reading the "
                             "on-disk cache")

    monkeypatch.setattr(oc.subprocess, "run", forbidden)
    assert runner.resolve_binary() == pinned_binary


def test_resolve_binary_writes_the_disk_cache(pinned_binary, monkeypatch):
    runner = _runner_for(pinned_binary, monkeypatch)
    runner.resolve_binary()
    on_disk = oc._read_version_cache()
    assert str(pinned_binary) in on_disk, "a verified binary was never recorded"
    assert on_disk[str(pinned_binary)][2] == oc.PINNED_OPENCODE_VERSION


def test_a_changed_binary_is_reverified_even_with_a_disk_entry(pinned_binary, monkeypatch):
    """The stamp is what makes the answer stale; a replaced binary must be
    probed again rather than trusted from the cache."""
    runner = _runner_for(pinned_binary, monkeypatch)
    runner.resolve_binary()
    _fresh_process()          # the disk entry survives: that is the point
    pinned_binary.write_text("#!/bin/sh\necho 0.0.1\n", encoding="utf-8")
    pinned_binary.chmod(0o755)
    with pytest.raises(RuntimeError, match="expected OpenCode"):
        runner.resolve_binary()


def test_a_wrong_version_still_fails_even_when_it_exits_zero(isolated, monkeypatch):
    binary = isolated / "wrong"
    binary.write_text("#!/bin/sh\necho 1.0.0\n", encoding="utf-8")
    binary.chmod(0o755)
    runner = _runner_for(binary, monkeypatch)
    with pytest.raises(RuntimeError, match="expected OpenCode"):
        runner.resolve_binary()


def test_a_nonzero_exit_is_still_a_failure_even_if_it_prints_the_pin(
        isolated, monkeypatch):
    """The strict check must survive the caching refactor: a binary that prints
    the right string on a failing exit is not a working runtime."""
    binary = isolated / "liar"
    binary.write_text(
        f"#!/bin/sh\necho {oc.PINNED_OPENCODE_VERSION}\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    runner = _runner_for(binary, monkeypatch)
    with pytest.raises(RuntimeError):
        runner.resolve_binary()


def test_an_undecodable_cache_file_does_not_break_the_probe(fake_binary):
    """`_read_version_cache` caught JSONDecodeError but not UnicodeDecodeError,
    so a corrupted cache raised out of probe_version — on the dispatch path,
    from an optimisation that must never be a reason to fail."""
    oc._version_cache_path().parent.mkdir(parents=True, exist_ok=True)
    oc._version_cache_path().write_bytes(b"\xff\xfe\x00not utf-8")
    _fresh_process()
    assert oc.probe_version(fake_binary) == "9.9.9"
    # And the unreadable file is replaced by a good one, so the next process is
    # served from disk rather than re-probing forever.
    assert str(fake_binary) in oc._read_version_cache()
