"""Concurrent manifest updates must not lose each other.

Two installs each read, modify and atomically write the manifest. Atomic means
the file is never torn — it does NOT mean the second writer saw the first's
change. Without a lock, last-writer-wins silently discards a host's whole
configuration.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pilot_workers.cli import install as install_mod


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOT_WORKERS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_a_slow_writer_does_not_clobber_a_fast_one(isolated):
    """The decisive shape for a lost update.

    Repeated idempotent writers converge even WITHOUT a lock — each keeps
    re-asserting its own entry, so a hammer-style test passes either way and
    lies. The loss is only permanent when one writer holds a stale read ACROSS
    another's entire write. Confirmed by disabling the flock and re-running this:
    claude survives, codex vanishes.
    """
    reading_done = threading.Event()

    def slow() -> None:
        with install_mod.manifest_transaction() as installs:
            install_mod.add_host_provider(installs, "claude", "glm")
            reading_done.set()
            time.sleep(0.4)  # hold the stale read open across the other write

    def fast() -> None:
        reading_done.wait(timeout=2)
        time.sleep(0.05)  # land inside the slow writer's window
        with install_mod.manifest_transaction() as installs:
            install_mod.add_host_provider(installs, "codex", "ds")

    threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    installs = json.loads(
        (isolated / "install-manifest.json").read_text(encoding="utf-8"))["installs"]
    assert "claude" in installs, "the slow writer's change was lost"
    assert "codex" in installs, "the fast writer's change was clobbered"


@pytest.mark.parametrize("argv,uninstall", [
    (["glm", "on", "claude"], False),
    (["glm", "on", "claude", "for", "code"], False),
    (["claude"], False),
    (["all"], False),
    (["glm", "on", "claude"], True),
    (["for", "code", "on", "claude"], True),
    (["claude"], True),
])
def test_every_manifest_read_happens_under_the_lock(isolated, monkeypatch,
                                                    argv, uninstall):
    """Every manifest read made by THESE command paths happens under the lock.

    Scope, stated honestly: the parametrize list below is the set of mutating
    commands, not every caller of ``_load_manifest`` (``status`` reads it
    outside a transaction on purpose — a reader needs no lock). A new mutating
    command must be added to the list, or its reads go unchecked.

    This replaced a source grep for the literal ``_load_manifest(manifest_path)``
    which the real call site does not even spell that way: it matched nothing, so
    any bare read-modify-write would have passed.
    """
    import fcntl

    real_load = install_mod._load_manifest
    held = {"count": 0}
    real_flock = fcntl.flock

    def tracking_flock(descriptor, operation):
        result = real_flock(descriptor, operation)
        if operation == fcntl.LOCK_EX:
            held["count"] += 1
        elif operation == fcntl.LOCK_UN:
            held["count"] -= 1
        return result

    def checked_load(path):
        assert held["count"] > 0, (
            "the manifest was read outside manifest_transaction")
        return real_load(path)

    monkeypatch.setattr("fcntl.flock", tracking_flock)
    monkeypatch.setattr(install_mod, "_load_manifest", checked_load)

    target = str(isolated / "target")
    # Seed something to remove, under the same instrumentation.
    if uninstall:
        install_mod.main(["glm", "on", "claude", "for", "code", "--target", target])
        install_mod.uninstall_main(argv)
    else:
        install_mod.main([*argv, "--target", target])
    assert held["count"] == 0, "a transaction exited without releasing the lock"


def test_transaction_writes_once_and_returns_the_change(isolated):
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
    data = json.loads((isolated / "install-manifest.json").read_text(encoding="utf-8"))
    assert data["installs"]["claude"]["providers"] == ["glm"]


def test_a_failing_transaction_writes_nothing(isolated):
    with pytest.raises(RuntimeError, match="deliberate"):
        with install_mod.manifest_transaction() as installs:
            install_mod.add_host_provider(installs, "claude", "glm")
            raise RuntimeError("deliberate")
    assert not (isolated / "install-manifest.json").exists(), (
        "a failed transaction left a partial manifest")


def test_the_lock_is_released_after_a_failure(isolated):
    with pytest.raises(RuntimeError):
        with install_mod.manifest_transaction():
            raise RuntimeError("boom")
    # Must not deadlock: a released lock lets the next transaction proceed.
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")


def test_a_crashed_holder_does_not_block_forever(isolated):
    """flock rather than a pid file precisely so the kernel cleans up: a stale
    lock file left by a dead process must not make the manifest unwritable."""
    lock = isolated / "install-manifest.json.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale", encoding="utf-8")

    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")

    data = json.loads((isolated / "install-manifest.json").read_text(encoding="utf-8"))
    assert data["installs"]["claude"]["providers"] == ["glm"]


def test_an_uninstall_deletes_an_emptied_manifest(isolated):
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
    with install_mod.manifest_transaction(delete_when_empty=True) as installs:
        del installs["claude"]
    assert not (isolated / "install-manifest.json").exists()


def test_an_install_keeps_an_emptied_manifest(isolated):
    """Only uninstall deletes. An install that merely purged legacy entries must
    leave a v4 file, or the next run re-migrates what was already migrated."""
    with install_mod.manifest_transaction() as installs:
        install_mod.add_host_provider(installs, "claude", "glm")
    with install_mod.manifest_transaction() as installs:
        del installs["claude"]
    assert (isolated / "install-manifest.json").is_file()
