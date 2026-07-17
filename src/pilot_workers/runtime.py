#!/usr/bin/env python3
"""Isolated execution runtime: environment, credentials, worktrees, process I/O."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, TextIO

from pilot_workers.providers import Provider, profile_paths

SAFE_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "SHELL", "PATH", "TMPDIR",
    "LANG", "LC_ALL",
    "JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT",
    "FLUTTER_ROOT", "GOPATH", "GOROOT",
    "CARGO_HOME", "RUSTUP_HOME",
    "NVM_DIR", "PYENV_ROOT", "RBENV_ROOT",
    "BUN_INSTALL", "PNPM_HOME",
)

HEARTBEAT_SECONDS = 60
TERMINATE_GRACE_SECONDS = 10


def ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def build_environment(provider: Provider, config: dict[str, Any]) -> dict[str, str]:
    paths = profile_paths(provider)
    for name in ("root", "config", "data", "state", "cache"):
        ensure_private_directory(paths[name])
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if os.environ.get(key)}
    env.update({
        "XDG_CONFIG_HOME": str(paths["config"]),
        "XDG_DATA_HOME": str(paths["data"]),
        "XDG_STATE_HOME": str(paths["state"]),
        "XDG_CACHE_HOME": str(paths["cache"]),
        "OPENCODE_CONFIG_DIR": str(paths["config"] / "opencode"),
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":")),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_AUTO_SHARE": "false",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
        "NO_COLOR": "1",
        "CI": "1",
    })
    return env


def credential_key(provider: Provider) -> str:
    path = profile_paths(provider)["auth"]
    if not path.is_file():
        raise RuntimeError(f"credential missing for {provider.key}; run: pilot-workers configure {provider.key}")
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"credential file is not private (expected mode 0600): {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read credential from {path}: {exc}") from exc
    entry = payload.get(provider.provider_id)
    if not isinstance(entry, dict) or entry.get("type") != "api":
        raise RuntimeError(f"credential file lacks API auth for {provider.provider_id}: {path}")
    key = entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError(f"credential is empty for {provider.provider_id}: {path}")
    return key


def credential_metadata(provider: Provider) -> dict[str, Any]:
    path = profile_paths(provider)["auth"]
    configured = False
    secure_mode = False
    if path.is_file():
        secure_mode = (path.stat().st_mode & 0o077) == 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entry = payload.get(provider.provider_id)
            configured = (
                isinstance(entry, dict)
                and entry.get("type") == "api"
                and isinstance(entry.get("key"), str)
                and bool(entry["key"].strip())
            )
        except (OSError, json.JSONDecodeError):
            configured = False
    return {"configured": configured, "secure_mode": secure_mode, "path": str(path)}


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
    from datetime import datetime, timezone
    import secrets as secrets_module
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
        raise RuntimeError(f"workdir {workdir} is not inside repository {repository_root}") from exc
    return target / relative


def session_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"sessionID", "sessionId", "session_id"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(session_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(session_ids(item))
    return found


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

    def _guard(self, action) -> None:
        if self._broken or self._writer is None:
            return
        try:
            action()
        except Exception as exc:
            self._broken = True
            print(f"note: live log rendering disabled ({exc})", file=sys.stderr)

    def event(self, event: dict[str, Any]) -> None:
        self._guard(lambda: self._writer.write_event(event))

    def raw_line(self, line: str) -> None:
        def action() -> None:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return
            self._writer.write_event(event)
        self._guard(action)

    def finalize(self) -> None:
        self._guard(lambda: self._writer.finalize())


def run_process(
    command: list[str], env: dict[str, str], task: str,
    log_path: Path, stderr_path: Path, secret: str,
    renderer: Any = None, timeout_s: int = 0, idle_timeout_s: int = 0,
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
                safe_renderer.raw_line(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ids = session_ids(event)
                if ids:
                    result.session_id = ids[-1]

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
                else:
                    next_heartbeat_silence = max(next_heartbeat_silence, HEARTBEAT_SECONDS)
        except KeyboardInterrupt:
            result.interrupted = True; stop_child()
            result.exit_code = process.returncode or 130
        finally:
            for thread in threads:
                thread.join(timeout=5)

    if (result.timed_out or result.idle_timed_out or result.interrupted) and result.exit_code == 0:
        result.exit_code = 124
    return result
