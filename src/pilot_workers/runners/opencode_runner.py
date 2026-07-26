"""OpenCode runner adapter — concrete Runner for the opencode-ai CLI.

Owns all OpenCode-specific logic: config schema, env vars, CLI flags, event
translation, and credential format. Upper layers delegate to this adapter.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from pilot_workers import policy
from pilot_workers.providers import (
    Provider,
    pilot_home,
    profile_paths,
    profile_root,
)
from pilot_workers.runners.base import (
    VERSION_PROBE_TIMEOUT_S,
    Runner,
    TokenUsage,
    ToolCall,
    UnifiedEvent,
)

# Pinned engine version. Single source of truth — install_runtime.sh and
# everything else reads it from here.
PINNED_OPENCODE_VERSION = "1.18.4"

# D6: cache for the ``--version`` check, mapping binary path ->
# (mtime_ns, size, version string). An entry is valid only while the file's
# (mtime_ns, size) is unchanged; a version mismatch is never cached by
# resolve_binary. Cleared after a successful ``install runner``.
_VERSION_CACHE: dict[Path, tuple[int, int, str]] = {}


def clear_version_cache() -> None:
    """Invalidate every remembered ``--version`` answer, both layers.

    Its one production caller is ``install runner``, whose comment says a fresh
    install invalidates any cached result. This used to clear the in-memory
    half only, which was harmless while nothing on the dispatch path read the
    disk half — and stopped being harmless the moment ``resolve_binary`` did.
    Stamps (mtime_ns, size) make a reinstalled binary's entry stale anyway; this
    is the explicit invalidation the caller asks for, not a substitute for them.
    """
    _VERSION_CACHE.clear()
    try:
        _version_cache_path().unlink()
    except (OSError, RuntimeError):
        # A cache is an optimisation; failing to clear it is never a reason to
        # fail the install that just succeeded.
        pass


def _version_cache_path() -> Path:
    from pilot_workers import providers

    return providers.pilot_home() / "runner-versions.json"


def _read_version_cache() -> dict:
    # ValueError, not just JSONDecodeError: a truncated or non-UTF-8 cache file
    # raised UnicodeDecodeError straight out of probe_version, which is on the
    # dispatch path — and this cache is an optimisation that must never be the
    # reason anything fails.
    try:
        data = json.loads(_version_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_version_cache(data: dict) -> None:
    # Shared writer: a unique temp name (one fixed name let a second writer
    # truncate the file the first was about to rename in) plus the fsync this
    # copy never had.
    from pilot_workers import runtime

    runtime.atomic_write_text(
        _version_cache_path(), json.dumps(data), mode=0o600)


def _cached_version(binary: Path, stamp: tuple[int, int]) -> str | None:
    """Version already known for this exact binary, from memory then disk.

    Both layers, in one place: ``resolve_binary`` used to consult only the
    in-memory dict, which is empty in every fresh dispatch process — so the
    ~330ms Node startup the disk cache exists to remove was still paid on the
    one path that pays for it.
    """
    cached = _VERSION_CACHE.get(binary)
    if cached is not None and cached[:2] == stamp:
        return cached[2]
    entry = _read_version_cache().get(str(binary))
    if (isinstance(entry, list) and len(entry) == 3
            and tuple(entry[:2]) == stamp and isinstance(entry[2], str)):
        _VERSION_CACHE[binary] = (stamp[0], stamp[1], entry[2])
        return entry[2]
    return None


def _remember_version(binary: Path, stamp: tuple[int, int], version: str) -> None:
    """Record a version in both cache layers. Never a reason to fail."""
    _VERSION_CACHE[binary] = (stamp[0], stamp[1], version)
    on_disk = _read_version_cache()
    on_disk[str(binary)] = [stamp[0], stamp[1], version]
    try:
        _write_version_cache(on_disk)
    except OSError:
        pass


def probe_version(binary: Path) -> str | None:
    """Cached ``binary --version`` probe, shared by ``cli.status``.

    Cached BOTH in memory and on disk. The in-memory half is useless on its own:
    every dispatch is a fresh process, so each one paid a ~330ms Node subprocess
    to re-learn a version that had not changed. The key is what makes the answer
    stale — the binary's mtime and size.

    A failed, empty or timed-out probe is never cached, and a cache that cannot
    be read or written is skipped silently: it is an optimisation, never a
    reason to fail.
    """
    st = binary.stat()
    stamp = (st.st_mtime_ns, st.st_size)

    known = _cached_version(binary, stamp)
    if known is not None:
        return known

    try:
        proc = subprocess.run(
            [str(binary), "--version"], text=True, capture_output=True,
            check=False, stdin=subprocess.DEVNULL,
            timeout=VERSION_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # `status` shows "unknown" rather than hanging. Not cached: a binary
        # that was slow once must be re-probed, not remembered as unknown.
        return None
    version = (proc.stdout or proc.stderr).strip() or None
    if version is not None:
        _remember_version(binary, stamp, version)
    return version

# Substring in a tool error message that signals a permission-rule denial.
_PERMISSION_DENIED_MARK = "rule which prevents"

# Tools whose output is a file dump or plain confirmation; the renderer
# shows only the input brief and suppresses the output line.
SILENT_OUTPUT_TOOLS = {"read", "edit", "write", "list", "todowrite"}

_INPUT_LIMIT = 200


class OpenCodeRunner(Runner):
    """Adapter for the opencode-ai CLI (pinned to ``PINNED_OPENCODE_VERSION``)."""

    name = "opencode"

    # ------------------------------------------------------------------
    # config / command / env
    # ------------------------------------------------------------------

    def build_config(
        self, provider: Provider, mode: str, permission_profile: str | None = None,
    ) -> dict:
        # Thin pass-through to policy.build_config — single source of truth.
        return policy.build_config(
            provider, mode, permission_profile=permission_profile,
        )

    def build_command(
        self, binary: Path, provider: Provider, mode: str,
        workdir: Path, run_id: str, session: str | None,
    ) -> list[str]:

        command = [
            str(binary), "--pure", "run",
            "--model", provider.model,
            "--agent", policy.MODE_TO_AGENT[mode],
            "--format", "json",
            "--thinking",
            "--title", f"pilot-worker-{mode}-{run_id}",
            "--dir", str(workdir),
        ]
        if session:
            command.extend(["--session", session])
        return command

    def runner_environment(
        self, provider: Provider, config: dict,
        paths: dict[str, Path] | None = None,
    ) -> dict[str, str]:

        # Neutral keys (SAFE_ENV_KEYS, XDG_*, NO_COLOR,
        # CI) are added by the runtime layer, not here.
        if paths is None:
            paths = profile_paths(provider)
        return {
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
        }

    def format_task_input(self, task: str, mode: str) -> str:

        return f'<worker-task mode="{mode}">\n{task}\n</worker-task>'

    # ------------------------------------------------------------------
    # event translation
    # ------------------------------------------------------------------

    def parse_events(self, raw: dict) -> list[UnifiedEvent]:
        """Translate one raw OpenCode event into 0..n UnifiedEvents.

        Translates raw OpenCode events to UnifiedEvents:
          - cli/dispatch.parse_jsonl (step/text/tool/error branches)
          - fmt_events tool input/output brief extraction
          - runtime.run_process recursive session-id scan
        """
        events: list[UnifiedEvent] = []

        # Top-level timestamp → ts (epoch ms). bool excluded.
        ts_value = raw.get("timestamp")
        ts: int | None = None
        if isinstance(ts_value, (int, float)) and not isinstance(ts_value, bool):
            ts = int(ts_value)

        # Recursive session-id scan: emit one session event if any found.
        # runtime.run_process keeps the LAST id (ids[-1]); do the same here.
        ids = _session_ids(raw)
        if ids:
            events.append(UnifiedEvent(kind="session", ts=ts, session_id=ids[-1]))

        event_type = raw.get("type")

        if event_type == "step_finish":
            events.append(UnifiedEvent(
                kind="step",
                ts=ts,
                tokens=_extract_tokens(raw.get("part")),
            ))
        elif event_type == "text":
            events.append(UnifiedEvent(
                kind="text",
                ts=ts,
                text=_part_text(raw.get("part")),
            ))
        elif event_type == "reasoning":
            events.append(UnifiedEvent(
                kind="reasoning",
                ts=ts,
                text=_part_text(raw.get("part")),
            ))
        elif event_type == "tool_use":
            events.append(UnifiedEvent(
                kind="tool",
                ts=ts,
                tool=_extract_tool_call(raw.get("part") or {}),
            ))
        elif event_type == "error":
            events.append(UnifiedEvent(kind="error", ts=ts))
        # Anything else → emit nothing (the session event above, if any,
        # already covers the session-id extraction side-effect).

        return events

    # ------------------------------------------------------------------
    # binary / credentials
    # ------------------------------------------------------------------

    def runtime_root(self) -> Path | None:
        return pilot_home() / "worker-runtime" / "opencode"

    def probe_version(self, binary: Path) -> str | None:
        # The cached seam: a bare `--version` costs a ~330ms Node startup, and
        # `status` and every `resolve_binary` would each pay it.
        return probe_version(binary)

    @property
    def pinned_version(self) -> str | None:
        return PINNED_OPENCODE_VERSION

    def install_script(self) -> Path | None:
        import pilot_workers

        return (Path(pilot_workers.__file__).resolve().parent
                / "scripts" / "install_runtime.sh")

    def sandbox_credential_path(self, paths: dict[str, Path]) -> Path:
        # OpenCode reads XDG_DATA_HOME/opencode/auth.json; the sandbox points
        # XDG_DATA_HOME at paths["data"].
        return paths["data"] / "opencode" / "auth.json"

    def binary_path(self) -> Path | None:
        # Best-effort location used for dry-run display; not verified.
        root = self.runtime_root()
        assert root is not None
        return (
            root
            / PINNED_OPENCODE_VERSION
            / "node_modules"
            / ".bin"
            / "opencode"
        )

    def resolve_binary(self) -> Path:
        # Builds the path locally so this module is the single source of truth for the
        # runner's binary location.
        binary = self.binary_path()
        assert binary is not None
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(
                f"pinned OpenCode {PINNED_OPENCODE_VERSION} is missing; "
                "run: pilot-workers install runner opencode"
            )
        # D6: serve an unchanged binary from the cache when its version was
        # already verified against the pin — no --version subprocess. BOTH cache
        # layers: this path only ever ran in a fresh process, where the
        # in-memory dict is empty, so consulting it alone meant every dispatch
        # still paid the Node startup.
        st = binary.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if _cached_version(binary, stamp) == PINNED_OPENCODE_VERSION:
            return binary
        try:
            version = subprocess.run(
                [str(binary), "--version"], text=True, capture_output=True,
                check=False, stdin=subprocess.DEVNULL,
                timeout=VERSION_PROBE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # This runs before the started event and before run_process arms
            # --timeout, and `dispatch` waits on this child with a bare
            # proc.wait(): an unbounded probe hangs the dispatch with no output.
            raise RuntimeError(
                f"the pinned OpenCode binary did not answer --version within "
                f"{VERSION_PROBE_TIMEOUT_S}s: {binary}. The runtime looks "
                "broken; reinstall it with: pilot-workers install runner opencode"
            ) from None
        if (
            version.returncode != 0
            or version.stdout.strip() != PINNED_OPENCODE_VERSION
        ):
            # A failed check is never cached.
            actual = (version.stdout or version.stderr).strip()
            raise RuntimeError(
                f"expected OpenCode {PINNED_OPENCODE_VERSION}, "
                f"got {actual or 'unknown'}"
            )
        # Recorded on disk too, which is what makes the NEXT dispatch free. The
        # returncode check above stays here rather than moving into
        # probe_version: that helper reports whatever the binary printed, and a
        # binary printing the pinned string on a failing exit is not a runtime.
        _remember_version(binary, stamp, PINNED_OPENCODE_VERSION)
        return binary

    def credential_path(self, provider: Provider) -> Path:
        return profile_root(provider) / "data" / "opencode" / "auth.json"

    def credential_payload(self, provider: Provider, key: str) -> dict:
        return {provider.provider_id: {"type": "api", "key": key}}

    def parse_credential(self, provider: Provider, payload: dict) -> str:

        # See runtime.credential_key for the neutral-layer wrapper.
        entry = payload.get(provider.provider_id)
        if not isinstance(entry, dict) or entry.get("type") != "api":
            raise RuntimeError(
                f"credential file lacks API auth for {provider.provider_id}"
            )
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError(
                f"credential is empty for {provider.provider_id}"
            )
        return key


# ----------------------------------------------------------------------
# parse_events helpers
# ----------------------------------------------------------------------


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _safe_int_cache(cache: Any, field: str) -> int:
    if isinstance(cache, dict):
        return _safe_int(cache.get(field))
    return 0


def _extract_tokens(part: Any) -> TokenUsage:
    if not isinstance(part, dict):
        return TokenUsage()
    tk = part.get("tokens")
    if not isinstance(tk, dict):
        return TokenUsage()
    return TokenUsage(
        input=_safe_int(tk.get("input")),
        output=_safe_int(tk.get("output")),
        reasoning=_safe_int(tk.get("reasoning")),
        cache_read=_safe_int_cache(tk.get("cache"), "read"),
        cache_write=_safe_int_cache(tk.get("cache"), "write"),
    )


def _part_text(part: Any) -> str | None:
    if not isinstance(part, dict):
        return None
    text = part.get("text")
    return text if isinstance(text, str) else None


def _extract_tool_call(part: Any) -> ToolCall:
    if not isinstance(part, dict):
        part = {}
    state = part.get("state") or {}
    if not isinstance(state, dict):
        state = {}
    tool_name = part.get("tool", "?")
    if not isinstance(tool_name, str):
        tool_name = "?" if tool_name is None else str(tool_name)

    status_value = state.get("status")
    status = status_value if isinstance(status_value, str) else (
        "" if status_value is None else str(status_value)
    )

    input_brief = _tool_input_brief(state.get("input") or {})
    output_brief = _first_line(state.get("output") or "", _INPUT_LIMIT)

    error = None
    if status == "error":
        error_value = state.get("error")
        if isinstance(error_value, str):
            error = _first_line(error_value, _INPUT_LIMIT)
        elif error_value is not None:
            error = _first_line(str(error_value), _INPUT_LIMIT)

    is_permission_denied = (
        isinstance(error, str) and _PERMISSION_DENIED_MARK in error
    )
    silent_output = tool_name in SILENT_OUTPUT_TOOLS

    return ToolCall(
        name=tool_name,
        status=status,
        input_brief=input_brief,
        output_brief=output_brief,
        error=error,
        is_permission_denied=is_permission_denied,
        silent_output=silent_output,
    )
# ----------------------------------------------------------------------
# Migrated from fmt_events.py — private, behaviour preserved verbatim.
# ----------------------------------------------------------------------


def _trim(value: Any, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _short_path(value: str) -> str:
    """~ for home, and keep only the last 3 segments of long paths."""
    home = str(Path.home())
    if value.startswith(home):
        value = "~" + value[len(home):]
    if len(value) > 64 and "/" in value:
        parts = value.split("/")
        if len(parts) > 4:
            value = "…/" + "/".join(parts[-3:])
    return value


def _first_line(value: str, limit: int) -> str:
    """First informative line, never a raw newline. Skips XML-ish wrappers."""
    for line in str(value).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("<") and line.endswith(">"):
            continue  # <path>…</path> / <type>file</type> / <content> wrappers
        if line.startswith("/") and " " not in line:
            line = _short_path(line)
        return _trim(line, limit)
    return ""


def _tool_input_brief(tool_input: dict[str, Any]) -> str:
    for key in ("command", "filePath", "pattern", "path", "query", "url"):
        value = tool_input.get(key)
        if value:
            text = str(value)
            if key in ("filePath", "path"):
                text = _short_path(text)
            return _trim(text.replace("\n", " "), _INPUT_LIMIT)
    return _trim(json.dumps(tool_input, ensure_ascii=False), _INPUT_LIMIT) if tool_input else ""


def _session_ids(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"sessionID", "sessionId", "session_id"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_session_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_session_ids(item))
    return found
