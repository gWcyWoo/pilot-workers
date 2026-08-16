"""Runner abstraction: unified event model + Runner interface.

A runner is the execution carrier that takes a task contract and runs it
through a model. The unified event types below are the lingua franca that
dispatchers, renderers, and verdict logic consume; each concrete Runner
parses its engine's native events into this shape on the read side.

Design notes:

- `worker_runner.started/summary/heartbeat/verdict` are pilot-workers-owned
  events; they bypass `parse_events` and go straight to rendering/verdict.
- `kind="step"` must fire exactly once per engine step. The step cap is
  enforced in TWO places and a runner must satisfy at least one: `build_config`
  hard-stops the engine (OpenCode's `steps` option), and `runtime.run_process`
  terminates the child once `max_steps` step events have gone by. The read-side
  cap is not decoration — Claude Code's CLI has no engine-side turn limit at
  all (its agent `maxTurns` applies to subagents only, verified against 2.1.233
  on 2026-08-15), so for that runner it is the ONLY cap. STEPS_BY_MODE is
  calibrated to an API round-trip and MUST be re-checked when a new runner is
  added, because "one step" is the runner's definition to make.
- Runners that do not support session resume MUST raise RuntimeError on a
  non-None `session` argument to `build_command` rather than silently ignore.
- The on-disk JSONL always stores the runner's raw events; `parse_events`
  only translates on the read side.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


# A `--version` probe answers in milliseconds when the runtime is healthy. It
# runs on the dispatch path, ahead of the point where run_process arms
# --timeout/--idle-timeout, so an unbounded one hangs a whole dispatch with no
# output. Generous enough that a cold, slow machine never trips it.
VERSION_PROBE_TIMEOUT_S = 30


@dataclass(frozen=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class ToolCall:
    name: str
    status: str                    # runner-reported status, e.g. "completed" / "error"
    input_brief: str               # single-line human-readable summary, "" if none
    output_brief: str              # first informative output line, "" if none
    error: str | None              # error text when status == "error"
    is_permission_denied: bool     # whether the runner flagged a permission denial
    silent_output: bool            # True = renderer should hide the output line


@dataclass(frozen=True)
class UnifiedEvent:
    kind: Literal["step", "text", "reasoning", "tool", "error", "session"]
    ts: int | None = None          # epoch milliseconds of the raw event, None if absent
    text: str | None = None        # text/reasoning payload
    tokens: TokenUsage | None = None   # step usage
    tool: ToolCall | None = None       # tool invocation info
    session_id: str | None = None      # session id for kind="session"


class Runner(ABC):
    """Worker runner adapter.

    Contract (required reading for new runner implementations):

    - `worker_runner.started/summary/heartbeat/verdict` are pilot-workers-owned
      events; they do not flow through parse_events and reach rendering and
      verdict logic directly.
    - `kind="step"` must fire exactly once per engine step. Either build_config
      makes the engine hard-stop at its steps cap, or the run relies on
      `runtime.run_process`'s read-side cap — which is armed for every runner
      and is the only cap available to engines that expose no turn limit.
      STEPS_BY_MODE MUST be re-checked when a new runner is wired in.
    - Runners that do not support session resume MUST raise RuntimeError on a
      non-None `session` argument; they MUST NOT silently ignore it.
    - The on-disk JSONL always stores the runner's raw events; parse_events
      only translates on the read side.
    """

    name: str

    @abstractmethod
    def build_config(self, provider: Any, mode: str, permission_profile: str | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def build_command(self, binary: Path, provider: Any, mode: str,
                      workdir: Path, run_id: str, session: str | None,
                      config: dict | None = None) -> list[str]:
        """Argv for this run.

        ``config`` is the mapping ``build_config`` returned for this dispatch.
        It is passed because not every engine takes its configuration through
        the environment: OpenCode reads a JSON blob from
        ``OPENCODE_CONFIG_CONTENT`` and ignores this, while Claude Code takes
        its settings, system prompt and tool list as command-line arguments.
        A runner that needs it must REFUSE when it is None rather than rebuild
        it — a rebuild cannot see the run's ``--permissions`` profile, and
        dispatching with quietly different rules than the operator asked for is
        worse than failing.
        """

    def working_directory(self, workdir: Path) -> Path | None:
        """The cwd this engine's process needs, or None to leave it alone.

        Default None: OpenCode is told where to work with `--dir` and inherits
        whatever cwd the dispatcher had. An engine that takes its workspace
        from `process.cwd()` must return `workdir` here — otherwise the worker
        runs in pw9's own dispatch cwd, which lives under `$PILOT_WORKERS_HOME`
        and is therefore inside the credential-path deny. Found the hard way:
        the first end-to-end run reported the project as empty and every read
        as "denied by your permission settings".
        """
        return None

    def apply_credential(self, env: dict[str, str], secret: str) -> None:
        """Place the provider credential into the child environment.

        Default: nothing — an engine that reads a credential FILE (OpenCode)
        needs no environment entry, and the sandbox symlink has already put the
        file where it looks. Overridden by engines that authenticate from the
        environment instead. Called by ``cli/run`` with the key it has already
        read, after ``build_environment``.
        """

    @abstractmethod
    def runner_environment(self, provider: Any, config: dict,
                           paths: dict | None = None) -> dict[str, str]:
        """Only the env vars specific to this runner; neutral parts
        (SAFE_ENV_KEYS / XDG) are owned by the runtime layer."""

    @abstractmethod
    def format_task_input(self, task: str, mode: str) -> str:
        """Wrap the task text into the engine's expected first-turn input
        (delivered via stdin)."""

    @abstractmethod
    def parse_events(self, raw: dict) -> list[UnifiedEvent]:
        """Translate one raw event into 0..n unified events.
        Unrecognized events return []."""

    @abstractmethod
    def resolve_binary(self) -> Path:
        """Locate and verify the runner executable; raise RuntimeError if it
        is missing or its version does not match."""

    @abstractmethod
    def credential_path(self, provider: Any) -> Path: ...

    @abstractmethod
    def credential_payload(self, provider: Any, key: str) -> dict[str, Any]:
        """Produce the credential-file payload structure; the actual file
        write (atomic write / 0600 mode) is owned by the neutral layer."""

    @abstractmethod
    def parse_credential(self, provider: Any, payload: dict) -> str:
        """Extract the API key from a credential-file payload; raise
        RuntimeError if the shape does not match."""

    def binary_path(self) -> Path | None:
        """Best-effort binary location WITHOUT verification (for dry-run display).
        Default: None (unknown until resolve_binary)."""
        return None

    # ------------------------------------------------------------------
    # Runtime installation and sandbox layout
    #
    # These four existed only as OpenCode literals scattered through the
    # neutral layer: the sandbox credential path was spelled
    # `data/opencode/auth.json` inside `runtime.provision_run_sandbox`, and
    # `install runner <name>` / `uninstall runner <name>` ignored their own
    # `name` argument and operated on OpenCode's directory whatever it said.
    # A second runner could not have been added without editing both files.
    # ------------------------------------------------------------------

    def sandbox_credential_path(self, paths: dict[str, Path]) -> Path:
        """Where inside a per-run sandbox this runner reads its credential.

        Given ``providers.run_paths(...)``. The neutral layer creates the
        parent directory and the symlink to the canonical credential; only the
        location is the runner's business. Default: the canonical path's name
        directly under the sandbox's data dir.
        """
        return paths["data"] / "credentials.json"

    def runtime_root(self) -> Path | None:
        """Directory holding this runner's installed runtime, if it has one.

        Used by `install runner` / `uninstall runner` and by the
        post-uninstall report. Default: None (nothing to install).
        """
        return None

    @property
    def pinned_version(self) -> str | None:
        """The exact runtime version this runner requires, if it pins one."""
        return None

    def probe_version(self, binary: Path) -> str | None:
        """This runner's installed version, or None if it cannot be determined.

        Default: run ``<binary> --version``. A runner that can cache the answer
        (the probe costs a Node startup for OpenCode) overrides this.

        Bounded, and with no stdin: a runtime that never answers must report
        "unknown", not block its caller.
        """
        import subprocess

        try:
            proc = subprocess.run(
                [str(binary), "--version"], text=True, capture_output=True,
                check=False, stdin=subprocess.DEVNULL,
                timeout=VERSION_PROBE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return None
        return (proc.stdout or proc.stderr).strip() or None

    def install_script(self) -> Path | None:
        """A script that installs this runner's runtime, if that is how it is
        installed. Default: None — `install runner <name>` then reports that
        this runner needs no runtime."""
        return None
