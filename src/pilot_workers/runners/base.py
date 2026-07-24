"""Runner abstraction: unified event model + Runner interface.

A runner is the execution carrier that takes a task contract and runs it
through a model. The unified event types below are the lingua franca that
dispatchers, renderers, and verdict logic consume; each concrete Runner
parses its engine's native events into this shape on the read side.

Design notes:

- `worker_runner.started/summary/heartbeat/verdict` are pilot-workers-owned
  events; they bypass `parse_events` and go straight to rendering/verdict.
- `kind="step"` must fire exactly once per engine step; `build_config` must
  hard-stop the engine at its step cap. STEPS_BY_MODE is currently calibrated
  to OpenCode's step granularity and MUST be re-calibrated when a new runner
  is added.
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
    - `kind="step"` must fire exactly once per engine step; build_config must
      make the engine hard-stop at its steps cap. STEPS_BY_MODE is currently
      calibrated to OpenCode's step granularity and MUST be re-calibrated
      when a new runner is wired in.
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
                      workdir: Path, run_id: str, session: str | None) -> list[str]: ...

    @abstractmethod
    def runner_environment(self, provider: Any, config: dict) -> dict[str, str]:
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
