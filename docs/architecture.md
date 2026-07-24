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
| `pilot-workers dispatch` | Wraps `run` as a subprocess; stdout = exactly two JSON lines (`started` + structured `verdict`, schema v2). AI planners use this. |
| `pilot-workers run` | Streaming entry: `started` → engine events → `summary`. Humans `tail -f latest.log` against this. |
| `pilot-workers template <mode>` | Prints a structured task template (code/explore/test/review). |
| `pilot-workers fanout` | Dispatches several jobs concurrently; stdout = one JSON array of verdicts. Hardened: `start_new_session` + SIGINT/SIGTERM `killpg` + per-job watchdog; exit 0 requires every verdict in `("completed","step_capped_partial")` AND `timed_out == idle_timed_out == interrupted == false` for all jobs. |
| `pilot-workers install <host\|all>` | Deploys the host playbook skill (`integrations/<host>-host/skills/pilot-workers/`); manifest v3 (host-level; first v3 install auto-migrates v1/v2, purging legacy per-provider files). |
| `pilot-workers install runner <name>` | Installs a worker runtime (e.g. OpenCode via npm). |
| `pilot-workers status [--json]`, `status <host>` | Credential, host-level install, and runner status overview. Surfaces provider `strengths`/`suitable_modes`/`notes`. |
| `pilot-workers credentials <key>` | Interactive credential setup (atomic write, 0600). |
| `pilot-workers maintain (logs\|runs\|worktrees)` | Log cleanup, run-sandbox reaper (`runs --older-than-days N [--keep M]`), and worktree lifecycle. |

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
| Per-run sandbox (`providers/<key>/runs/<run_id>/{config,data,state}` 0700 + cache symlink + `auth.json` symlink + `.lock`) | Concurrent workers (incl. resume) colliding on the same XDG state; the SQLite-WAL session store is per-run by construction |
| Credential file 0600 enforcement | Group/world-readable key files |
| `redact()` on stdout/stderr | Key appearing in logs |
| Task via stdin (not argv) | Task content in process list |
| `--worktree` detached git worktree | Parallel workers stepping on each other's files |
| timeout + idle-timeout + heartbeat | Stuck worker running forever |

This is profile and process isolation, not an OS sandbox. For untrusted repositories, add a container.

**Per-run sandbox lifecycle**: each dispatch calls `runtime.provision_run_sandbox()` — it creates the private 0700 `config/data/state` dirs, symlinks `cache` to the shared per-provider cache, symlinks `data/opencode/auth.json` to the canonical credential (zero-copy), and acquires `root/.lock` (`O_CREAT|O_EXCL`, JSON `{pid, started_at}`). Staleness = pid dead OR the live pid's start time no longer matches (Linux `/proc/<pid>/stat` field 22; macOS `LC_TIME=C ps -o lstart=`); falls back to pid-dead-only if the platform source is unavailable. Resume (`--session` + `--run-id`) reuses the ORIGINAL run's sandbox and acquires the lock only when absent or stale; a live lock is a loud "run is still active" error. The resume window equals the sandbox retention window — once `maintain runs` reaps the sandbox, resume refuses with "session expired past retention; redispatch cold".

### 5. Provider registry (`providers.py`)

- Loads all `data/providers/*.yaml` at import time.
- 7 required fields: `key`, `provider_id`, `model_id`, `base_url`, `display_name`, `context_tokens`, `output_tokens`.
- Optional: `runner` (default `opencode`), `permissions` (profile name), `asset_prefix` (default = key), and the v0.5.0 metadata fields `strengths` / `suitable_modes` / `notes` (free-form strings surfaced by `pilot-workers status`).
- Reserved keys: `runner`, `all`, `on`, `claude`, `codex`.
- `pilot_home()` resolution: `$PILOT_WORKERS_HOME` → `$CODEX_HOME` → `~/.codex`.

## Data flow

1. Planner generates a task file (from `pilot-workers template <mode>`) and calls `pilot-workers dispatch --provider <key> --mode <mode> --workdir <path> --task-file <file>`.
2. `dispatch` spawns `run` as a subprocess.
3. `run` resolves the provider's runner, builds engine config, creates an isolated env, and spawns the engine binary.
4. Task text is delivered via stdin (XML-wrapped for OpenCode).
5. Engine events stream through: raw lines are logged to JSONL on disk, translated to `UnifiedEvent`s for rendering (`latest.log`) and session ID extraction.
6. On engine exit, `dispatch` parses the JSONL via the runner adapter, extracts the structured result block from the worker's final text (per-mode schema; `parse_state` = `parsed` / `unstructured` / `unavailable`), classifies the verdict (`completed` / `step_capped_partial` / `empty` / `error`), writes `report.md` + `verdict.json` atomically (0600), and prints the verdict to stdout. Sibling flags `timed_out` / `idle_timed_out` / `interrupted` are independent of the verdict string and MUST be checked even on `completed`.
7. Planner reads the two-line stdout (`started` + `verdict`) and acts on the verdict (consuming `verdict.result` first; `final_text_path` at most once, only when `parse_state != "parsed"` or a finding needs surrounding context).

## Structured verdict (schema v2)

The stdout `verdict` line and the on-disk `<run_id>.verdict.json` carry `schema_version: 2`:

- `parse_state`: `parsed` | `unstructured` | `unavailable` — whether the final text held a valid per-mode result block.
- `result`: the per-mode schema dict, or `null`:
  - `explore`: `{facts: [{fact, file_line}], truncated: bool, more_in: [dirs]}`
  - `code`: `{status, files_changed: [], validation: {commands: [{cmd, exit_code, output_summary}], passed: bool}, remaining_risks}`
  - `test`: `{command, passed: int, failed: int, failures: [{test, error}]}` (`error` capped at 40 lines)
  - `review`: `{overall, severity_counts: {high, medium, low}, findings: [{severity, file_line, summary, impact, suggested_fix}]}`
  - `resume`: reuses the `code` schema.
- `final_text_path`: path to `<run_id>.report.md` (verbatim last text event, or the `no model output` sentinel). `final_text` is gone from the verdict.
- Sibling flags `timed_out` / `idle_timed_out` / `interrupted` are set independently; the authoritative empty signal is `parse_state == "unavailable"`, never report content.

Extraction is deterministic (no LLM): find the LAST `<!--PILOT_RESULT_BEGIN-->...<!--PILOT_RESULT_END-->` block in the final text, validate against the per-mode schema. The block is injected by `prompts/*.md`. `dispatch --reparse <jsonl> --mode <mode>` recomputes a verdict from an existing JSONL and also writes `report.md` (idempotent).

Synthesized fanout verdicts (`synthesized: true`, `reason` ∈ `crash|timeout|idle_timeout|interrupted`, `result = null`, `final_text_path = null`, plus `stderr_tail`) are emitted only when a fanout child produced no verdict line on stdout; consume `reason` + `stderr_tail` only.

Classification order (frozen, full-matrix tested): (1) `steps >= cap` → `step_capped_partial`; (2) `parse_state == "parsed"` → `completed`; (3) summary-error AND `parse_state != "parsed"` → `error` (plus the empty-text + error-event fallback); (4) `parse_state != "parsed"` AND `len(final_text) >= threshold` → `completed`; (5) else → `empty`.

## Fanout hardening

`pilot-workers fanout` spawns each dispatch child with `start_new_session=True` (pid == pgid) and tracks every child group. Hardening:

- Main-thread SIGINT and SIGTERM handlers record `reason = "interrupted"` into shared per-job state BEFORE `killpg`-ing every tracked child group (SIGTERM → `TERMINATE_GRACE_SECONDS` → SIGKILL). A second SIGINT restores `SIG_DFL` (hard-kill escape). Handlers return; exactly one JSON array is printed.
- Per-job watchdog: deadline = `--timeout` + `TERMINATE_GRACE_SECONDS` + harvest allowance; silence ≥ grace past the deadline triggers a kill; an absolute `MAX_EPILOGUE_SECONDS` ceiling kills regardless of activity. `--timeout 0` disables the parent watchdog for that job (the child's `--idle-timeout` remains the liveness bound).
- Single-writer rule: only `_run_job` writes `results[index]`; killers write `reasons[index]` only. Reason precedence: a real child verdict line wins verbatim; a synthesized verdict is emitted only when no verdict line was captured (the recorded reason is logged to stderr regardless).
- Exit code 0 requires every verdict in `("completed", "step_capped_partial")` AND `timed_out == idle_timed_out == interrupted == false` for all jobs. `--max-parallel` is validated ≥ 1.

## Install manifest (schema v3)

```json
{
  "schema_version": 3,
  "installs": {
    "claude": {
      "installed_at": "...", "package_version": "...",
      "files": [...], "created_dirs": [...]
    },
    "codex": {...}
  }
}
```

- Host-level tracking (one entry per host; the provider dimension is gone). Atomic write after each host (`os.replace` so no v1/v2 file survives to be re-migrated).
- v1 (`hosts` key) and v2 (per-provider nesting) manifests are migrated on first v3 install: the v1 `__all__` entry is purged FIRST via `_purge_entry`, then every v2 provider entry, printing one `removed:` line per purged file.
- `uninstall` removes files + empty `created_dirs` (deepest first). Missing files are skipped silently.

## Built-in providers

| Key | Provider ID | Model | Context / Output | Endpoint |
|---|---|---|---|---|
| `glm` | `glm-worker` | `glm-5.2` | 1,000,000 / 131,072 | `open.bigmodel.cn/api/coding/paas/v4` |
| `kimi-k3` | `kimi-worker` | `k3` | 1,048,576 / 1,048,576 | `api.kimi.com/coding/v1` |
| `ds` | `ds-worker` | `deepseek-v4-pro` | 1,000,000 / 384,000 | `api.deepseek.com/v1` |

All use OpenCode's `@ai-sdk/openai-compatible` adapter. Provider/model/endpoint cannot be overridden by tasks.

## Host integrations

ONE `pilot-workers` playbook skill is deployed per host with `pilot-workers install <host|all>`:

- **Claude Code** (`claude-host/skills/pilot-workers/`): a doctrine playbook (Quick Reference + per-mode sections) installed into `~/.claude/skills/pilot-workers/`.
- **Codex** (`codex-host/skills/pilot-workers/`): the same playbook skill installed into `$CODEX_HOME/skills/pilot-workers/`.

The skill is a playbook, not CLI syntax docs: it carries the dispatch doctrine (axis-splitting, worth-it self-checks, cross-model review for rewrite-scale diffs, fanout recipes, `--worktree` for parallel code jobs, resume-first recovery, verdict-state handling matrix, dispatch-from-main-session-only). The v0.4.0 12-agents + 8-commands matrix is gone. Integration files reference the public CLI interface (`pilot-workers template`, `pilot-workers dispatch`) — they carry no engine-specific knowledge.

## Security model

- Credentials: stored at `pilot_home()/opencode-workers/providers/<key>/data/opencode/auth.json`, mode 0600. Written atomically (tempfile + fsync + rename). Format/path owned by the runner adapter, file IO by the neutral layer.
- Per-run sandbox: each dispatch provisions `providers/<key>/runs/<run_id>/{config,data,state}` (0700) plus a symlink to the shared per-provider cache and a zero-copy symlink to the canonical `auth.json` (0600 check follows the symlink). A JSON `{pid, started_at}` `.lock` (`O_CREAT|O_EXCL`) prevents two dispatches from grabbing the same sandbox; resume (`--session` + `--run-id`) reuses the original sandbox and the resume window equals the sandbox retention window. The reaper (`maintain runs`) lstat-walks every sandbox and unlinks symlinks rather than following them (the canonical credential and shared cache always survive).
- Environment: `SAFE_ENV_KEYS` whitelist + `_PROTECTED_KEYS` filter. Runner-specific env (e.g. `OPENCODE_*`) merged last but cannot override protected keys.
- Config delivery: `OPENCODE_CONFIG_CONTENT` env var (highest precedence in OpenCode). No API keys in the config — keys travel only via the engine's own credential file.
- Task files: templates carry a "never include credentials" ban. Task text enters the engine via stdin, never argv.
- Log redaction: `redact()` replaces any occurrence of the provider key in stdout/stderr with `[REDACTED]`.

## Testing

289 pytest tests, all offline:
- No network calls, no real `~/.claude` or `~/.codex` access.
- Install tests use `PILOT_WORKERS_HOME` + `--target` pointing to `tmp_path`.
- Covers: provider loading, policy matrices, runner adapter translation, render equivalence, dispatch verdict classification + per-mode result schemas, manifest v3 migration (v1/v2 purge ordering, `os.replace`), install/uninstall lifecycle, status command, CLI routing, credential handling, runtime isolation, per-run sandbox provisioning + lock staleness, fanout watchdog/signal/reason-precedence.
