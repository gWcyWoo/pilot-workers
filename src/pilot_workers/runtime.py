#!/usr/bin/env python3
"""Isolated execution runtime: environment, credentials, worktrees, process I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets as secrets_module
import subprocess
import sys
import threading
import time
from typing import Any, TextIO

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


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


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
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                text=True, capture_output=True, check=False, env=env,
            )
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
    except OSError:
        return True
    started_at = process_start_time(pid)
    if started_at is None:
        return False
    return started_at != lock.get("started_at")


def acquire_run_lock(root: Path) -> None:
    """Create ``root/.lock`` (O_CREAT|O_EXCL) with {pid, started_at}.

    A live lock raises RuntimeError ("run is still active"); a stale lock is
    unlinked and acquisition retried once.
    """
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


def release_run_lock(root: Path) -> None:
    """Remove the sandbox lockfile; a missing lockfile is not an error."""
    try:
        (root / ".lock").unlink()
    except OSError:
        pass


def provision_run_sandbox(provider: Provider, run_id: str, runner: Runner) -> dict[str, Path]:
    """Provision a per-run sandbox and hold its lock.

    Creates the private 0700 root/config/data/state dirs, links ``cache`` to
    the shared per-provider cache, links ``data/opencode/auth.json`` to the
    canonical credential (zero-copy; the reaper unlinks, never follows), and
    acquires the run lock. Returns ``providers.run_paths(provider, run_id)``.
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
        if not paths["cache"].exists():
            os.symlink(str(shared_cache), str(paths["cache"]))
        opencode_data = paths["data"] / "opencode"
        opencode_data.mkdir(parents=True, exist_ok=True)
        auth_link = opencode_data / "auth.json"
        if not auth_link.exists() and not auth_link.is_symlink():
            os.symlink(str(runner.credential_path(provider)), str(auth_link))
    except Exception:
        release_run_lock(paths["root"])
        raise
    return paths


def credential_key(provider: Provider, runner: Runner) -> str:
    path = runner.credential_path(provider)
    if not path.is_file():
        raise RuntimeError(f"credential missing for {provider.key}; run: pilot-workers credentials {provider.key}")
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"credential file is not private (expected mode 0600): {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read credential from {path}: {exc}") from exc
    return runner.parse_credential(provider, payload)


def credential_metadata(provider: Provider, runner: Runner) -> dict[str, Any]:
    path = runner.credential_path(provider)
    configured = False
    secure_mode = False
    if path.is_file():
        secure_mode = (path.stat().st_mode & 0o077) == 0
        try:
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
        return value.replace(secret, "[REDACTED]") if secret else value

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
