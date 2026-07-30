#!/usr/bin/env python3
"""Isolated execution runtime: environment, credentials, worktrees, process I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import contextlib
import json
import os
from pathlib import Path
import secrets as secrets_module
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, TextIO

from pilot_workers import providers as providers_module
from pilot_workers.providers import Provider, profile_paths, run_paths
from pilot_workers.runners.base import Runner, UnifiedEvent

SAFE_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR",
    "LANG", "LC_ALL",
    "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "FLUTTER_ROOT", "GOPATH", "GOROOT",
    "CARGO_HOME", "RUSTUP_HOME",
    "NVM_DIR", "PYENV_ROOT", "RBENV_ROOT",
    "BUN_INSTALL", "PNPM_HOME",
)

# Env keys this tool's OWN child processes (dispatch -> run) may inherit: the
# same whitelist the worker gets, plus the two path overrides this tool itself
# reads. Passing the parent's whole environment instead would copy any API key
# the user exported in their shell into every orchestrator process, where a
# core dump or /proc/<pid>/environ exposes it — for no benefit, since the
# worker's own environment is rebuilt from SAFE_ENV_KEYS regardless.
ORCHESTRATOR_ENV_KEYS = SAFE_ENV_KEYS + ("PILOT_WORKERS_HOME", "CODEX_HOME")

# Env keys a runner must never override: neutral SAFE_ENV_KEYS already owned
# by this layer, the XDG_*_HOME dirs (also owned here), plus the NO_COLOR / CI
# flags the runtime sets to keep worker output deterministic.
_PROTECTED_KEYS = frozenset(SAFE_ENV_KEYS) | frozenset(
    k for k in ("NO_COLOR", "CI")
) | frozenset(
    f"XDG_{d}_HOME" for d in ("CONFIG", "DATA", "STATE", "CACHE")
)

HEARTBEAT_SECONDS = 60
TERMINATE_GRACE_SECONDS = 10

# `ps` answers in milliseconds. The probe runs on the lock path, so it is
# bounded for the same reason the runner version probe is: nothing has armed a
# timeout yet at that point.
START_TIME_PROBE_TIMEOUT_S = 10

# Below this length a "secret" is short enough to occur in ordinary output, so
# replacing it would corrupt the text it is supposed to protect. Real provider
# keys are far longer. Same threshold taskguard uses for its exact-match scan.
MIN_REDACTABLE_SECRET = 12


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def orchestrator_environment() -> dict[str, str]:
    """Env for this tool's own child processes, filtered to what they need.

    ``dispatch`` spawns ``run``, and ``fanout`` spawns ``dispatch``; neither
    needs the parent's exported secrets. Prefer ``child_environment`` — it adds
    the import pinning every one of those children also needs.
    """
    return {key: os.environ[key]
            for key in ORCHESTRATOR_ENV_KEYS if os.environ.get(key)}


def child_cwd() -> str:
    """A directory this tool controls, for spawning its own children in.

    ``python -m`` puts cwd at ``sys.path[0]``, AHEAD of PYTHONPATH, so anything
    named like a stdlib or package module dropped in a world-writable cwd by any
    local process would be imported in preference to the real one.
    """
    root = providers_module.pilot_home() / "dispatch-cwd"
    # 0700 like every other directory this tool owns. Ownership already keeps
    # other users out of a directory under this user's home, so the mode is not
    # what carries the guarantee — but a cwd whose whole purpose is "nobody else
    # can drop a module here" should not be the one place left at the umask.
    ensure_private_directory(root)
    return str(root)


def child_environment() -> dict[str, str]:
    """Env for a ``python -m pilot_workers...`` child of this tool.

    Two jobs, both belonging to whoever spawns a child of ours, which is why
    they live together here rather than half in a CLI module: carry over only
    the variables the child needs (see ORCHESTRATOR_ENV_KEYS), and pin the
    package it imports.

    The child resolves ``pilot_workers`` from its own sys.path — site-packages,
    not necessarily the tree this process was imported from. When the two differ
    (an editable checkout under test alongside an older installed copy) the
    child silently executes different code and everything downstream validates
    the wrong thing.
    """
    import pilot_workers

    package_root = str(Path(pilot_workers.__file__).resolve().parent.parent)
    env = orchestrator_environment()
    # Belt and braces with child_cwd: refuse to put cwd on sys.path at all.
    env["PYTHONSAFEPATH"] = "1"
    existing = env.get("PYTHONPATH", "")
    if package_root not in existing.split(os.pathsep):
        env["PYTHONPATH"] = (
            package_root + os.pathsep + existing if existing else package_root)
    return env


def build_environment(
    provider: Provider,
    runner_env: dict[str, str],
    paths: dict[str, Path] | None = None,
) -> dict[str, str]:
    """Compose the child process environment.

    Neutral concerns (SAFE_ENV_KEYS whitelist + XDG dirs) are owned here; the
    runner-specific variables come from ``runner.runner_environment`` via the
    ``runner_env`` argument and are merged unchanged.

    ``paths`` overrides the XDG targets: given a per-run sandbox mapping
    (``providers.run_paths``), the XDG dirs point at the sandbox; omitted,
    the shared provider profile is used (and created) as before.
    """
    if paths is None:
        paths = profile_paths(provider)
        for name in ("root", "config", "data", "state", "cache"):
            ensure_private_directory(paths[name])
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if os.environ.get(key)}
    env.update({
        "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "XDG_STATE_HOME": str(paths["state"]),
        "XDG_CACHE_HOME": str(paths["cache"]),
        "NO_COLOR": "1",
        "CI": "1",
    })
    # Drop any runner-supplied entry that would shadow a neutral key — those
    # are owned by this layer and must remain deterministic.
    filtered = {k: v for k, v in runner_env.items() if k not in _PROTECTED_KEYS}
    env.update(filtered)
    return env


# ---------------------------------------------------------------------------
# Per-run sandbox (D5): process start times, run locks, provisioning
# ---------------------------------------------------------------------------


def process_start_time(pid: int) -> str | None:
    """Opaque token identifying the process incarnation at ``pid``.

    Linux: /proc/<pid>/stat field 22 (parsed after the LAST ')' because the
    comm field may contain spaces/parens). macOS: locale-pinned
    ``LC_TIME=C ps -o lstart= -p <pid>``. Returns None on any failure.
    """
    try:
        if sys.platform == "darwin":
            env = dict(os.environ)
            env["LC_TIME"] = "C"
            # Bounded, and with no stdin: this runs on the lock path, where an
            # unbounded probe would hang a dispatch before anything is armed.
            # A timeout lands on the same "unavailable" answer as any other
            # failure, which lock_is_stale already handles conservatively.
            try:
                result = subprocess.run(
                    ["ps", "-o", "lstart=", "-p", str(pid)],
                    text=True, capture_output=True, check=False, env=env,
                    stdin=subprocess.DEVNULL, timeout=START_TIME_PROBE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return None
            if result.returncode != 0:
                return None
            token = result.stdout.strip()
            return token or None
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = text[text.rindex(")") + 1:].split()
        # fields[0] is field 3 (state); starttime is field 22 → index 19.
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def read_run_lock(root: Path) -> dict[str, Any] | None:
    """Read the sandbox lockfile; None when absent or unreadable."""
    try:
        payload = json.loads((root / ".lock").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def lock_is_stale(lock: dict[str, Any]) -> bool:
    """A lock is stale when the pid is dead or was recycled since locking.

    When the platform start-time source is unavailable, fall back to
    pid-dead-only.
    """
    pid = lock.get("pid")
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # The process exists, this user just cannot signal it. A bare
        # `except OSError` read that as dead: `lock_is_stale({"pid": 1})`
        # returned True for launchd. Not reachable while every sandbox is 0700
        # under one user's home — a foreign pid implies a foreign-owned sandbox
        # nobody else can write — but "exists" must never answer "gone".
        return False
    except OSError:
        return True
    recorded = lock.get("started_at")
    if recorded is None:
        # The probe failed when this lock was WRITTEN, so there is nothing to
        # compare against. The pid is alive (os.kill above); "cannot compare"
        # must never read as "the holder is gone". Once the probe recovered, a
        # live sandbox was declared stale and taken over by a second run - the
        # one thing this lock exists to prevent.
        return False
    started_at = process_start_time(pid)
    if started_at is None:
        return False
    return started_at != recorded


def run_guard_path(root: Path) -> Path:
    """Serialization guard for one sandbox's lock, kept OUTSIDE the sandbox.

    Creating a file inside the sandbox would bump the sandbox directory's
    mtime, and ``maintain runs`` decides both retention and its keep-set order
    from that mtime — so merely checking a lock would postpone the reaping of
    the runs most in need of it. The reaper removes the guard along with the
    sandbox it belongs to.
    """
    return root.parent / f".{root.name}.lock.guard"


@contextlib.contextmanager
def _guard_held(root: Path):
    """Hold an exclusive flock on the sandbox's guard file.

    Not re-entrant: flock is per open-file-description, so a second ``os.open``
    of the same guard in this process would block on itself. Everything that
    needs the guard therefore calls the ``_locked`` helpers below from inside
    exactly one of these blocks.
    """
    import fcntl

    guard = os.open(run_guard_path(root), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(guard, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(guard, fcntl.LOCK_UN)
        finally:
            os.close(guard)


def _acquire_run_lock_locked(root: Path) -> None:
    """Create ``root/.lock``; the caller must hold the guard flock."""
    lock_path = root / ".lock"
    payload = json.dumps({
        "pid": os.getpid(),
        "started_at": process_start_time(os.getpid()),
    })
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = read_run_lock(root)
            if existing is not None and not lock_is_stale(existing):
                raise RuntimeError(
                    f"run is still active (pid {existing.get('pid')}): {root}"
                )
            try:
                lock_path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return
    raise RuntimeError(f"cannot acquire run lock: {lock_path}")


def acquire_run_lock(root: Path) -> None:
    """Create ``root/.lock`` (O_CREAT|O_EXCL) with {pid, started_at}.

    A live lock raises RuntimeError ("run is still active"); a stale lock is
    unlinked and acquisition retried once.

    The whole attempt runs under an exclusive flock on the sandbox's guard
    file: judging a lock stale and unlinking it are otherwise two steps, and a
    second acquirer completing a full takeover in between would have its
    fresh LIVE lock unlinked by the first — two runs sharing one sandbox.
    The guard is never deleted by an acquirer; the kernel drops the flock with
    the process, so a crashed holder cannot block anyone.

    The lock is held by the FILE, so it stops protecting the sandbox the moment
    something deletes that file. An operation that destroys the sandbox must use
    ``run_lock_held`` instead.
    """
    with _guard_held(root):
        _acquire_run_lock_locked(root)


@contextlib.contextmanager
def run_guard_held(root: Path):
    """Hold the sandbox's guard flock WITHOUT taking the run lock.

    For a caller that only needs to read a lock and act on the answer — log
    cleanup does exactly that, and its check-then-act window is the one
    ``run_lock_held`` closes for the reaper. Taking the run lock here would be
    wrong twice over: it leaves a ``.lock`` behind in a sandbox that survives,
    and a concurrent resume would then be refused rather than made to wait.
    Under this guard a resume either finishes first (so the caller sees its live
    lock) or waits for the caller to finish.
    """
    with _guard_held(root):
        yield


@contextlib.contextmanager
def run_lock_held(root: Path):
    """Take the run lock and keep the guard flock for the whole block.

    ``acquire_run_lock`` releases the guard on return, which is right for a run
    that then works inside the sandbox — its ``.lock`` file stands in for it.
    A reaper has no such stand-in: it DELETES ``.lock`` early in its walk, and
    from that instant a concurrent resume could acquire the sandbox being
    deleted (and the reaper's final ``rmdir`` would fail on the files that
    resume recreated). Holding the flock across the whole operation keeps the
    lock the single arbiter it is meant to be.
    """
    with _guard_held(root):
        _acquire_run_lock_locked(root)
        yield


def release_run_lock(root: Path) -> None:
    """Remove the sandbox lockfile; a missing lockfile is not an error."""
    try:
        (root / ".lock").unlink()
    except OSError:
        pass


def provision_run_sandbox(provider: Provider, run_id: str, runner: Runner) -> dict[str, Path]:
    """Provision a per-run sandbox and hold its lock.

    Creates the private 0700 root/config/data/state dirs, links ``cache`` to
    the shared per-provider cache, links the runner's own credential location
    (``runner.sandbox_credential_path``) to the canonical credential (zero-copy;
    the reaper unlinks, never follows), and acquires the run lock. Returns
    ``providers.run_paths(provider, run_id)``.

    The location used to be the hardcoded ``data/opencode/auth.json``; the
    docstring outlived the seam that replaced it.
    """
    from pilot_workers.providers import runs_root as _runs_root
    paths = run_paths(provider, run_id)
    ensure_private_directory(_runs_root(provider))
    ensure_private_directory(paths["root"])
    acquire_run_lock(paths["root"])
    try:
        for name in ("config", "data", "state"):
            ensure_private_directory(paths[name])
        shared_cache = profile_paths(provider)["cache"]
        ensure_private_directory(shared_cache)
        # `exists()` follows the link, so a DANGLING cache symlink reads as
        # absent and os.symlink then raises FileExistsError. The auth link six
        # lines below already checks both; this is the same guard, which is the
        # seventh sibling site this session.
        if not paths["cache"].exists() and not paths["cache"].is_symlink():
            os.symlink(str(shared_cache), str(paths["cache"]))
        # WHERE the credential goes inside the sandbox is the runner's business
        # (it must match where the engine looks); creating the link is ours.
        auth_link = runner.sandbox_credential_path(paths)
        auth_link.parent.mkdir(parents=True, exist_ok=True)
        if not auth_link.exists() and not auth_link.is_symlink():
            os.symlink(str(runner.credential_path(provider)), str(auth_link))
    except Exception:
        release_run_lock(paths["root"])
        raise
    return paths


def credential_key(provider: Provider, runner: Runner, *,
                   require_private: bool = True) -> str:
    """Read a provider's API key.

    ``require_private=False`` reads the key from a file whose mode is wider than
    0600 instead of refusing. Only ``configured_secrets`` passes it: that list
    exists to REDACT keys, and an insecurely stored key is the one most likely
    to leak, so refusing to read it dropped it from the redaction list exactly
    when redaction mattered most. Every dispatch-path caller keeps the refusal —
    a key that cannot be stored safely is not used.
    """
    path = runner.credential_path(provider)
    if not path.is_file():
        # The remedy text lives in providers.credential_setup_hint — one copy.
        raise RuntimeError(
            f"credential missing for {provider.key}; "
            f"{providers_module.credential_setup_hint(provider.key)}"
        )
    try:
        # Inside the try with the read, like its sibling credential_metadata:
        # a credential removed between is_file() above and here raised a bare
        # OSError instead of this function's own "cannot read" message. The
        # round-18 fix reached the sibling and not this one — eighth instance
        # this session of a property fixed at one site only.
        insecure = require_private and bool(path.stat().st_mode & 0o077)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read credential from {path}: {exc}") from exc
    if insecure:
        raise RuntimeError(f"credential file is not private (expected mode 0600): {path}")
    if not isinstance(payload, dict):
        # ``parse_credential`` is typed for a mapping and reaches straight for
        # ``.get``; valid JSON that is a bare string or list would raise
        # AttributeError from inside the runner — an uncaught traceback rather
        # than the "your credential file is malformed" this is.
        raise RuntimeError(
            f"credential file is not a JSON object: {path}")
    return runner.parse_credential(provider, payload)


def credential_metadata(provider: Provider, runner: Runner) -> dict[str, Any]:
    path = runner.credential_path(provider)
    configured = False
    secure_mode = False
    if path.is_file():
        try:
            # Inside the try with the read: a credential rewritten or removed
            # between is_file() and here raised OSError straight out of
            # fanout's preflight, where every other failure gives one clean line.
            secure_mode = (path.stat().st_mode & 0o077) == 0
            payload = json.loads(path.read_text(encoding="utf-8"))
            configured = _looks_configured(provider, runner, payload)
        except (OSError, json.JSONDecodeError):
            configured = False
    return {"configured": configured, "secure_mode": secure_mode, "path": str(path)}


def _looks_configured(provider: Provider, runner: Runner, payload: Any) -> bool:
    """Best-effort 'has a usable API key' check; never raises."""
    try:
        key = runner.parse_credential(provider, payload)
    except (RuntimeError, TypeError, AttributeError):
        return False
    return bool(key.strip())


def create_detached_worktree(workdir: Path, worktree_parent: Path) -> Path:
    root_result = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, check=False,
    )
    if root_result.returncode != 0:
        raise RuntimeError(f"--worktree requires a Git repository: {root_result.stderr.strip()}")
    repository_root = Path(root_result.stdout.strip()).resolve()
    status = subprocess.run(
        ["git", "-C", str(repository_root), "status", "--porcelain"],
        text=True, capture_output=True, check=False,
    )
    if status.returncode != 0:
        raise RuntimeError(f"cannot inspect Git status: {status.stderr.strip()}")
    if status.stdout.strip():
        raise RuntimeError("--worktree requires a clean repository (commit or stash first)")
    ensure_private_directory(worktree_parent)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = worktree_parent / f"{repository_root.name}-{stamp}-{secrets_module.token_hex(3)}"
    add_result = subprocess.run(
        ["git", "-C", str(repository_root), "worktree", "add", "--detach", str(target), "HEAD"],
        text=True, capture_output=True, check=False,
    )
    if add_result.returncode != 0:
        raise RuntimeError(f"cannot create detached worktree: {add_result.stderr.strip()}")
    try:
        relative = workdir.resolve().relative_to(repository_root)
    except ValueError as exc:
        subprocess.run(
            ["git", "-C", str(repository_root), "worktree", "remove", "--force", str(target)],
            capture_output=True, check=False,
        )
        raise RuntimeError(f"workdir {workdir} is not inside repository {repository_root}") from exc
    return target / relative


def configured_secrets() -> list[str]:
    """Every provider key configured on this machine, for exact-match scanning.

    Exact matching is precise and false-positive free, so it complements
    taskguard's shape patterns and is the only way to redact a key this tool
    holds but did not pass to the current run. Unreadable or unconfigured
    providers are skipped: a guard must never be the reason a dispatch fails.

    ``require_private=False`` on purpose. Refusing to read a key stored at 0644
    removed it from this list, so the one key most exposed to begin with was
    also the one that reached the planner unredacted.
    """
    from pilot_workers.runners import get_runner

    found: list[str] = []
    for provider in providers_module.PROVIDERS.values():
        try:
            found.append(credential_key(provider, get_runner(provider.runner),
                                        require_private=False))
        except (OSError, RuntimeError, KeyError):
            continue
    return found


def redact_secrets(text: str, secrets: list[str]) -> str:
    """Replace every known secret in ``text``. Short values are ignored.

    ``run_process`` redacts only the key it was handed, which is right for the
    live stream but leaves every OTHER configured key intact — and a worker's
    stderr can mention one. Anything that carries child output somewhere new
    must pass it through here first.
    """
    for secret in secrets:
        if secret and len(secret) >= MIN_REDACTABLE_SECRET:
            text = text.replace(secret, "[REDACTED]")
    return text


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600,
                      prefix: str | None = None) -> None:
    """Replace ``path`` with ``text`` in one step, at ``mode``.

    One implementation for every file this tool rewrites in place. There were
    four, and they had already drifted — different chmod placement, and one with
    no fsync at all — so a crash-safety fix reached whichever copy the author
    happened to be looking at.

    The mode is applied to the temp file BEFORE the rename, so the file is never
    visible at its final name with wider permissions. No temp file is left
    behind on success or failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=prefix or (path.name + "."), suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def open_private_text(path: Path) -> TextIO:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


@dataclass
class RunResult:
    exit_code: int
    session_id: str | None
    timed_out: bool = False
    idle_timed_out: bool = False
    interrupted: bool = False


class _SafeRenderer:
    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self._broken = False

    def _guard(self, action: Any) -> None:
        if self._broken or self._writer is None:
            return
        try:
            action()
        except Exception as exc:
            self._broken = True
            print(f"note: live log rendering disabled ({exc})", file=sys.stderr)

    def event(self, event: dict[str, Any]) -> None:
        self._guard(lambda: self._writer.write_event(event))

    def raw_line(self, line: str, runner: Runner | None) -> None:
        def action() -> None:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            if runner is None:
                # Without a runner we can only render self-owned events.
                self._writer.write_event(event)
                return
            for ev in runner.parse_events(event):
                self._writer.write_unified(ev)
            # Self-owned events (worker_runner.*) live outside parse_events;
            # also render them in case the line is one.
            self._writer.write_event(event)
        self._guard(action)

    def finalize(self) -> None:
        self._guard(lambda: self._writer.finalize())


def run_process(
    command: list[str], env: dict[str, str], task: str,
    log_path: Path, stderr_path: Path, secret: str,
    renderer: Any = None, timeout_s: int = 0, idle_timeout_s: int = 0,
    runner: Runner | None = None,
) -> RunResult:
    safe_renderer = _SafeRenderer(renderer)
    result = RunResult(exit_code=1, session_id=None)
    last_activity = time.monotonic()
    started_at = last_activity
    lock = threading.Lock()

    def redact(value: str) -> str:
        # Same floor as redact_secrets and taskguard: below it a "secret" occurs
        # in ordinary text and replacing it corrupts what it protects. This copy
        # had no floor at all.
        if not secret or len(secret) < MIN_REDACTABLE_SECRET:
            return value
        return value.replace(secret, "[REDACTED]")

    with open_private_text(log_path) as stdout_log, open_private_text(stderr_path) as stderr_log:
        process = subprocess.Popen(
            command, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

        def feed_stdin() -> None:
            try:
                assert process.stdin is not None
                process.stdin.write(task)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        def touch() -> None:
            nonlocal last_activity
            with lock:
                last_activity = time.monotonic()

        def consume_stdout() -> None:
            assert process.stdout is not None
            for raw_line in process.stdout:
                touch()
                line = redact(raw_line)
                stdout_log.write(line); stdout_log.flush()
                sys.stdout.write(line); sys.stdout.flush()
                safe_renderer.raw_line(line, runner)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or runner is None:
                    continue
                try:
                    for ev in runner.parse_events(event):
                        if ev.kind == "session" and ev.session_id:
                            result.session_id = ev.session_id
                except Exception:
                    pass

        def consume_stderr() -> None:
            assert process.stderr is not None
            for raw_line in process.stderr:
                touch()
                line = redact(raw_line)
                stderr_log.write(line); stderr_log.flush()
                sys.stderr.write(line); sys.stderr.flush()

        threads = [
            threading.Thread(target=feed_stdin, daemon=True),
            threading.Thread(target=consume_stdout, daemon=True),
            threading.Thread(target=consume_stderr, daemon=True),
        ]
        for thread in threads:
            thread.start()

        def stop_child() -> None:
            process.terminate()
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()

        def emit_runner_event(event: dict[str, Any]) -> None:
            payload = json.dumps(event)
            stderr_log.write(payload + "\n"); stderr_log.flush()
            sys.stderr.write(payload + "\n"); sys.stderr.flush()
            safe_renderer.event(event)

        next_heartbeat_silence = HEARTBEAT_SECONDS
        try:
            while True:
                try:
                    result.exit_code = process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                with lock:
                    silent = now - last_activity
                elapsed = now - started_at
                if timeout_s and elapsed >= timeout_s:
                    result.timed_out = True; stop_child()
                    result.exit_code = process.returncode or 124; break
                if idle_timeout_s and silent >= idle_timeout_s:
                    result.idle_timed_out = True; stop_child()
                    result.exit_code = process.returncode or 124; break
                if silent >= next_heartbeat_silence:
                    emit_runner_event({"type": "worker_runner.heartbeat",
                                       "elapsed_s": int(elapsed), "silent_s": int(silent)})
                    next_heartbeat_silence = silent + HEARTBEAT_SECONDS
        except KeyboardInterrupt:
            result.interrupted = True; stop_child()
            result.exit_code = process.returncode or 130
        finally:
            for thread in threads:
                thread.join(timeout=5)

    if (result.timed_out or result.idle_timed_out or result.interrupted) and result.exit_code == 0:
        result.exit_code = 124
    return result
