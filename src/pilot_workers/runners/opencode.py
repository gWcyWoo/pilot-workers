"""OpenCode runner implementation — the first (and currently only) runner.

Wraps opencode-ai CLI (pinned version) with provider-isolated profiles,
credential management, stdin task delivery, and structured event output.
"""

from __future__ import annotations

from pilot_workers.providers import PINNED_OPENCODE_VERSION, runtime_binary


def verify_binary() -> str:
    """Return the path to the verified OpenCode binary, or raise."""
    import os
    import subprocess
    binary = runtime_binary()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(
            f"pinned OpenCode {PINNED_OPENCODE_VERSION} is missing; "
            "run: bash scripts/install_runtime.sh"
        )
    version = subprocess.run(
        [str(binary), "--version"], text=True, capture_output=True, check=False
    )
    if version.returncode != 0 or version.stdout.strip() != PINNED_OPENCODE_VERSION:
        actual = (version.stdout or version.stderr).strip()
        raise RuntimeError(
            f"expected OpenCode {PINNED_OPENCODE_VERSION}, got {actual or 'unknown'}"
        )
    return str(binary)
