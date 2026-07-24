# pilot-workers — Architecture

## Overview

```
Planner (Claude / Codex / any host)
   │ writes task file, calls pilot-workers dispatch
   ▼
┌─────────────────────────────────────────────────┐
│  pilot-workers (pip package)                    │
│                                                 │
│  cli/dispatch.py ─── two-line JSON contract     │
│       │              (started + verdict)        │
│  cli/run.py ──────── streaming events           │
│       │                                         │
│  runners/ ────────── adapter layer              │
│  ├─ base.py          Runner ABC + UnifiedEvent  │
│  └─ opencode_runner  OpenCode implementation    │
│       │                                         │
│  policy.py ───────── mode permissions + config  │
│  runtime.py ──────── process isolation          │
│  providers.py ────── YAML-driven routing        │
└─────────────────────────────────────────────────┘
   │ subprocess (sanitized env, stdin task)
   ▼
OpenCode 1.18.4 ──→ GLM / Kimi / DeepSeek (official APIs)
```

## Layers

### 1. CLI layer

| Entry | Purpose |
|---|---|
| `pilot-workers dispatch` | Wraps `run` as a subprocess; stdout = exactly two JSON lines (`started` + `verdict`). AI planners use this. |
| `pilot-workers run` | Streaming entry: `started` → engine events → `summary`. Humans `tail -f latest.log` against this. |
| `pilot-workers template <mode>` | Prints a structured task template (code/explore/test/review). |
| `pilot-workers install <provider\|all> on <host\|all>` | Deploys integration files per provider × host, tracked in manifest v2. |
| `pilot-workers install runner <name>` | Installs a worker runtime (e.g. OpenCode via npm). |
| `pilot-workers status [--json]` | Credential, install, and runner status overview. |
| `pilot-workers credentials <key>` | Interactive credential setup (atomic write, 0600). |

### 2. Runner adapter layer (`runners/`)

`base.py` defines the contract:

- **`UnifiedEvent`** (kind: step/text/reasoning/tool/error/session) — the only event type the upper layers consume.
- **`Runner` ABC** — 11 methods covering config generation, command assembly, environment injection, task formatting, event translation, binary resolution, and credential management.

`opencode_runner.py` is the OpenCode implementation. It owns all OpenCode-specific knowledge: config schema (`$schema: opencode.ai/config.json`), `OPENCODE_*` env vars, CLI flags (`--pure run --format json --thinking`), event format (step_finish/text/tool_use/error with part.tokens.cache), permission-denied detection (`"rule which prevents"`), auth.json format, and the pinned binary version.

Contract rules (see `base.py` docstring):
- Self events (`worker_runner.*`) bypass adapters — they are pilot-workers' own format.
- Disk JSONL always stores raw engine events; adapters translate on the read side.
- `started`/`verdict` events carry a `runner` field so logs are self-describing.
- `--runner` on `dispatch --reparse` selects the adapter for historical logs.

### 3. Policy layer (`policy.py`)

Mode → agent mapping, shell permission matrices, prompt assembly. Currently OpenCode-specific encoding (last-match-wins rule ordering), invoked only through `OpenCodeRunner.build_config()`. A future runner encodes the same mode intent in its own format.

- `STEPS_BY_MODE`: code/resume/review = 120, explore/test = 80.
- Permission profiles (`data/permissions/*.yaml`) override mode defaults via `_merge_permissions()`.

### 4. Isolation layer (`runtime.py`)

Runner-neutral process isolation:

| Mechanism | Prevents |
|---|---|
| `SAFE_ENV_KEYS` whitelist | Parent API keys leaking to worker |
| `_PROTECTED_KEYS` filter | Runner env overriding PATH/HOME/XDG |
| Per-provider XDG directory tree | Cross-provider credential/cache contamination |
| Credential file 0600 enforcement | Group/world-readable key files |
| `redact()` on stdout/stderr | Key appearing in logs |
| Task via stdin (not argv) | Task content in process list |
| `--worktree` detached git worktree | Parallel workers stepping on each other's files |
| timeout + idle-timeout + heartbeat | Stuck worker running forever |

This is profile and process isolation, not an OS sandbox. For untrusted repositories, add a container.

### 5. Provider registry (`providers.py`)

- Loads all `data/providers/*.yaml` at import time.
- 7 required fields: `key`, `provider_id`, `model_id`, `base_url`, `display_name`, `context_tokens`, `output_tokens`.
- Optional: `runner` (default `opencode`), `permissions` (profile name), `asset_prefix` (default = key, used for integration file naming).
- Reserved keys: `runner`, `all`, `on`, `claude`, `codex`.
- `pilot_home()` resolution: `$PILOT_WORKERS_HOME` → `$CODEX_HOME` → `~/.codex`.

## Data flow

1. Planner generates a task file (from `pilot-workers template <mode>`) and calls `pilot-workers dispatch --provider <key> --mode <mode> --workdir <path> --task-file <file>`.
2. `dispatch` spawns `run` as a subprocess.
3. `run` resolves the provider's runner, builds engine config, creates an isolated env, and spawns the engine binary.
4. Task text is delivered via stdin (XML-wrapped for OpenCode).
5. Engine events stream through: raw lines are logged to JSONL on disk, translated to `UnifiedEvent`s for rendering (`latest.log`) and session ID extraction.
6. On engine exit, `dispatch` parses the JSONL via the runner adapter, computes a verdict (`completed` / `step_capped_partial` / `empty` / `error`), writes it to disk and stdout.
7. Planner reads the two-line stdout (`started` + `verdict`) and acts on the verdict.

## Install manifest (schema v2)

```json
{
  "schema_version": 2,
  "installs": {
    "claude": {
      "glm": {"installed_at": "...", "package_version": "...", "files": [...], "created_dirs": [...]},
      "kimi-k3": {...}
    },
    "codex": {...}
  }
}
```

- Per (provider, host) pair tracking. Atomic writes after each pair.
- v1 manifests (`hosts` key) are migrated in-memory on load.
- `uninstall` removes files + empty `created_dirs` (deepest first). Missing files are skipped with a note.

## Built-in providers

| Key | Provider ID | Model | Context / Output | Endpoint |
|---|---|---|---|---|
| `glm` | `glm-worker` | `glm-5.2` | 1,000,000 / 131,072 | `open.bigmodel.cn/api/coding/paas/v4` |
| `kimi-k3` | `kimi-worker` | `k3` | 1,048,576 / 1,048,576 | `api.kimi.com/coding/v1` |
| `ds` | `ds-worker` | `deepseek-v4-pro` | 1,000,000 / 384,000 | `api.deepseek.com/v1` |

All use OpenCode's `@ai-sdk/openai-compatible` adapter. Provider/model/endpoint cannot be overridden by tasks.

## Host integrations

Installed per provider with `pilot-workers install <provider|all> on <host>`:

- **Claude Code** (`claude-host/`): 4 agents per provider (`{prefix}-coder/explorer/reviewer/tester`) + optional slash commands (`commands/{prefix}/code|explore|review|test`).
- **Codex** (`codex-host/`): 1 skill dir per provider (`{prefix}/SKILL.md + openai.yaml`).

Integration files reference the public CLI interface (`pilot-workers template`, `pilot-workers dispatch`) — they carry no engine-specific knowledge.

## Security model

- Credentials: stored at `pilot_home()/opencode-workers/providers/<key>/data/opencode/auth.json`, mode 0600. Written atomically (tempfile + fsync + rename). Format/path owned by the runner adapter, file IO by the neutral layer.
- Environment: `SAFE_ENV_KEYS` whitelist + `_PROTECTED_KEYS` filter. Runner-specific env (e.g. `OPENCODE_*`) merged last but cannot override protected keys.
- Config delivery: `OPENCODE_CONFIG_CONTENT` env var (highest precedence in OpenCode). No API keys in the config — keys travel only via the engine's own credential file.
- Task files: templates carry a "never include credentials" ban. Task text enters the engine via stdin, never argv.
- Log redaction: `redact()` replaces any occurrence of the provider key in stdout/stderr with `[REDACTED]`.

## Testing

130 pytest tests, all offline:
- No network calls, no real `~/.claude` or `~/.codex` access.
- Install tests use `PILOT_WORKERS_HOME` + `--target` pointing to `tmp_path`.
- Covers: provider loading, policy matrices, runner adapter translation, render equivalence, dispatch verdict classification, install/uninstall lifecycle, status command, CLI routing, credential handling, runtime isolation.
