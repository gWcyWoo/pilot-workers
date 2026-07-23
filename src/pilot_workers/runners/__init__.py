"""Runner abstraction layer.

A runner is the execution carrier that takes a task contract and runs it
through a model. OpenCode is the first implementation; others (Aider,
Continue, etc.) can be added by implementing the same interface.

The registry below is the single entry point: callers ask for a runner by
name via ``get_runner`` and receive a singleton ``Runner`` instance.
"""

from pilot_workers.runners.base import (
    Runner,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
)
from pilot_workers.runners.opencode_runner import OpenCodeRunner

RUNNERS: dict[str, Runner] = {"opencode": OpenCodeRunner()}


def get_runner(name: str) -> Runner:
    try:
        return RUNNERS[name]
    except KeyError:
        raise RuntimeError(
            f"unknown runner: {name} (available: {sorted(RUNNERS)})"
        )
