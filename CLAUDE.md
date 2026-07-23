# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

pilot-workers dispatches bounded tasks to isolated LLM workers (GLM, Kimi, etc.) via the OpenCode runtime. The main AI agent (Claude, Codex, or any planner) stays in control; the worker only executes what it's told. Provider routing is locked per YAML config — tasks cannot override model, endpoint, or credentials.

## Commands

```bash
# Install the pinned OpenCode runtime (one-time)
bash "$(python3 -c "from pathlib import Path; import pilot_workers; print(Path(pilot_workers.__file__).parent / 'scripts' / 'install_runtime.sh')")"

# Configure credentials (interactive, key never displayed)
python3 -m pilot_workers.credentials glm
python3 -m pilot_workers.credentials kimi-k3

# Run a task
python3 -m pilot_workers.cli.run \
  --provider glm --mode code \
  --workdir /path/to/project \
  --task-file /path/to/task.md

# Dry-run (print routing metadata, no model call)
python3 -m pilot_workers.cli.run --provider glm --mode explore --workdir . --task "hello" --dry-run

# Check credential status
python3 -m pilot_workers.credentials all --status

# Clean old logs (always keeps newest run)
python3 -m pilot_workers.maintain logs --older-than-days 7

# List/remove detached worktrees
python3 -m pilot_workers.maintain worktrees list
python3 -m pilot_workers.maintain worktrees remove /path/to/worktree

# Print the task template for a mode, fill it in, then dispatch
pilot-workers template code > /tmp/task.md
pilot-workers dispatch --provider glm --mode code --workdir /path/to/project --task-file /tmp/task.md
# dispatch stdout = exactly two JSON lines: worker_runner.started + worker_runner.verdict

# Install host integrations (agents, commands, skills)
# Overwrites same-named files; reinstall purges files from the previous install (tracked in
# $PILOT_WORKERS_HOME/install-manifest.json — uninstall uses the same manifest).
pilot-workers install claude   # → ~/.claude/agents/ + ~/.claude/commands/
pilot-workers install codex    # → $CODEX_HOME/skills/
pilot-workers install all      # both
pilot-workers uninstall claude # precise removal per manifest
```

```bash
# Set up dev environment (one-time) and run tests
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Tests live in `tests/` (100 tests, all offline — no network, no real `~/.claude`/`~/.codex` access).

## Architecture

**Data flow**: Provider YAML (with `runner` field) → `providers.py` loads at import → `runners/` registry resolves the runner adapter → adapter builds engine config → `runtime.py` spawns isolated subprocess with sanitized env → adapter translates raw engine events to `UnifiedEvent`s → `fmt_events.py` renders them to `latest.log`; `cli/dispatch.py` aggregates them into a verdict.

**Runner adapter layer** (`runners/`): engines are pluggable. `base.py` defines `UnifiedEvent` (kind: step/text/reasoning/tool/error/session) and the `Runner` ABC (config/command/env/task-input/event-translation/binary/credential seams). `opencode_runner.py` is the OpenCode implementation and owns everything OpenCode-specific: config schema, `OPENCODE_*` env vars, CLI flags, event format, permission-denied detection, auth.json format, `PINNED_OPENCODE_VERSION`, and the npm binary path. `RUNNERS` registry + `get_runner(name)` in `__init__.py`. Contract notes (see base.py docstring): self events (`worker_runner.*`) bypass adapters; disk JSONL always stores raw engine events (adapters translate on read); a `runner` field in started/verdict events makes logs self-describing; `--runner` on reparse selects the adapter for historical logs.

**Key modules in `src/pilot_workers/`**:

- `providers.py` — Loads all `data/providers/*.yaml` at import time into `PROVIDERS` dict (runner-neutral fields incl. optional `runner:`, default `opencode`). Path helpers (`pilot_home()`, `profile_paths()`) and `MAX_TASK_BYTES`.
- `policy.py` — Mode→agent mapping (`MODE_TO_AGENT`), `STEPS_BY_MODE`, shell permission matrices, prompt assembly from `prompts/*.md`, permission profile loading from `data/permissions/`, and `build_config()`. The encoding is OpenCode-specific (last-match-wins semantics; deny rules must come after allow rules they override) and is invoked only via `OpenCodeRunner.build_config`; a future runner encodes the same mode intent its own way.
- `runtime.py` — Runner-neutral isolation layer: `SAFE_ENV_KEYS` whitelist, XDG directory isolation per provider (runner env merged on top), credential file IO with 0600 enforcement (path/format from the runner), detached worktree creation, subprocess I/O with heartbeat/timeout/idle-timeout, and `[REDACTED]` replacement for leaked secrets.
- `cli/run.py` — Thin CLI entry point; resolves the provider's runner and delegates. Console script entry: `pilot-workers`.
- `cli/dispatch.py` — Deterministic outer shell (two-line stdout contract: started + verdict); verdict computed from UnifiedEvents.
- `cli/install.py` — Deploys host integration files (agents, commands, skills) to user config directories, records an install manifest for clean uninstall.
- `fmt_events.py` — Renders UnifiedEvents and self events into `latest.log` for `tail -f`. Monitor conventions: `== DONE` on success, `!! ` prefix on errors.
- `credentials.py` — Interactive credential setup (path/payload from the provider's runner) with atomic write (tempfile + fsync + rename → 0600).
- `maintain.py` — Log cleanup and worktree lifecycle. Refuses to delete dirty or unintegrated worktrees.

**Package data** (shipped with `pip install pilot-workers`):

- `data/providers/*.yaml` — Provider definitions (GLM, Kimi, DeepSeek).
- `data/permissions/*.yaml` — Permission profiles (relaxed, strict).
- `prompts/*.md` — Worker system prompts injected by the runner.
- `integrations/` — Host integration configs (claude-host agents/commands, codex-host skills).
- `scripts/install_runtime.sh` — OpenCode runtime installer.

**Modes and permissions**: Five modes (`code`, `explore`, `test`, `review`, `resume`). Each mode has built-in default permissions: `code` allows shell `*` with network/destructive denies; `explore` and `review` are read-only (shell default deny, explicit allow list for grep/find/git-read commands, `*>*` deny last to block redirects); `test` extends read-only with test runner commands; `resume` reuses `code` permissions with a required `--session`. Defaults can be overridden via **permission profiles** (see below).

**Provider isolation**: Each provider gets its own XDG tree under `$PILOT_WORKERS_HOME/opencode-workers/providers/<key>/` (config, data, state, cache). Credentials stored in `data/opencode/auth.json`. The subprocess inherits only `SAFE_ENV_KEYS` from the parent environment — no API keys leak across providers.

**Host integrations** (`integrations/`): Config-only directories (no Python code) for planner-side integration. `claude-host/` has 12 agents (glm/kimi/ds × coder/explorer/reviewer/tester) + 8 slash commands. `codex-host/` has skill entry points. Any host that can write a task file and call the CLI can integrate. Run `pilot-workers install claude` or `pilot-workers install codex` to deploy.

## Permission profiles

Built-in mode defaults can be overridden with YAML profiles in `data/permissions/`. Profiles define per-mode shell and tool permission overrides; unspecified rules keep their defaults. Merge order: built-in defaults → `_all` section → mode-specific section (last-match-wins for shell rules, direct overwrite for tool rules).

```yaml
# data/permissions/relaxed.yaml
_all:
  shell:
    "curl *": allow
  tools:
    webfetch: allow

code:
  shell:
    "wget *": allow
```

Apply a profile via CLI flag (highest priority) or provider YAML field:

```bash
python3 -m pilot_workers.cli.run --permissions relaxed --provider glm --mode code ...
```

```yaml
# data/providers/glm.yaml — optional field
permissions: relaxed
```

Requires `pyyaml`. Bundled profiles: `relaxed` (allows network tools/commands), `strict` (blocks rm/mv/chmod in code mode).

## Adding a new provider

Drop a YAML file in `data/providers/` with all 7 required fields (`key`, `provider_id`, `model_id`, `base_url`, `display_name`, `context_tokens`, `output_tokens`), then run `python3 -m pilot_workers.credentials <key>`. No Python changes needed. Optionally add `permissions: <profile-name>` to use a custom permission profile.

## Conventions

- Python >= 3.11, zero runtime dependencies (`pyyaml` is optional with a stdlib fallback parser).
- `providers.py` has a flat YAML fallback — provider files must stay flat key:value, no nesting.
- The `OPENCODE_CONFIG_CONTENT` env var carries the full config as JSON to the subprocess; this is the highest-precedence config path in OpenCode.
- Task text is delivered via stdin to OpenCode (`--pure run`), never in argv.
- `$PILOT_WORKERS_HOME` env var overrides the default root (`$CODEX_HOME` → `~/.codex`).

## Known issues

- `docs/architecture.md` directory tree references the old `~/.codex/skills/dispatch-opencode-workers/` layout, not the current `src/pilot_workers/` structure.
