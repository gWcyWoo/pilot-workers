# pilot-workers

Dispatch bounded tasks to isolated LLM workers. Your main AI agent (Claude, Codex, or any planner) stays in control of requirements, planning, and verification — the worker only executes what it's told.

## What it does

- **Provider isolation**: each model (GLM, Kimi, DeepSeek, or your own) gets its own credentials, XDG directories, logs, and session storage. No cross-contamination.
- **Fixed routing**: provider, model, and endpoint are locked per YAML config. Tasks cannot override them.
- **Security by default**: API keys never appear in CLI args, environment variables, task contracts, or logs. Output is auto-redacted.
- **Five modes**: `code` (edit), `explore` (read-only), `test` (run tests), `review` (read-only audit), `resume` (continue a prior code session).
- **Pluggable runners**: the runner adapter layer (`Runner` ABC) abstracts engine-specific details. Currently ships with OpenCode; designed for future alternatives.
- **Observable**: two-line JSON contract (`started` + structured `verdict` carrying `parse_state`, a per-mode `result`, and `final_text_path`) for AI planners; human-readable `latest.log` for `tail -f`.
- **Per-run sandboxes**: every dispatch runs inside its own isolated XDG tree (`providers/<key>/runs/<run_id>/`) with a zero-copy symlink to the canonical credential and a shared per-provider cache; resume (`--session` + `--run-id`) reuses the original sandbox.
- **Host-level playbook**: one `pilot-workers` playbook skill per host (Claude Code or Codex). `install claude` / `install codex` deploys the same engine-neutral playbook regardless of which providers you've configured.

## Install

```bash
pip install pilot-workers
```

## Quick start

```bash
# 1. Install the worker runtime
pilot-workers install runner opencode

# 2. Configure credentials (interactive, key never displayed)
pilot-workers credentials glm
pilot-workers credentials kimi-k3
pilot-workers credentials ds

# 3. Deploy the playbook skill to your host
pilot-workers install claude    # Claude Code: pilot-workers playbook skill
pilot-workers install codex     # Codex: same playbook skill
pilot-workers install all       # both hosts

# 4. Check everything is ready
pilot-workers status

# 5. Verify with a dry-run
pilot-workers run --provider glm --mode explore --workdir . --task "hello" --dry-run

# 6. Run a real task
pilot-workers template code > /tmp/task.md    # generate a structured task template
# fill in the template, then:
pilot-workers dispatch --provider glm --mode code --workdir /path/to/project --task-file /tmp/task.md
# dispatch stdout = exactly two JSON lines: worker_runner.started + worker_runner.verdict
```

## CLI reference

```
pilot-workers <subcommand> [args]

  run              Dispatch a task (streaming output).
  dispatch         Deterministic wrapper around run (two-line JSON: started + verdict).
  fanout           Dispatch several jobs concurrently; stdout = one JSON array of verdicts.
  template         Print the task template for a mode (code|explore|test|review).
  install          Install host playbook skill or runner.
                     install <host|all>
                     install runner <name>
  uninstall        Remove host playbook skill or runner.
                     uninstall <host|all>
                     uninstall runner <name>
  status           Show provider credentials, host installs, and runner state.
                     status [--json]
                     status <host>
  credentials      Configure isolated worker credentials.
  maintain         Worker log, run-sandbox, and worktree lifecycle tools.
                     maintain logs --older-than-days N
                     maintain runs --older-than-days N [--keep M]
                     maintain worktrees list|remove <path>
```

## Adding a new provider

Drop a YAML file in `data/providers/` (inside the package):

```yaml
key: my-model
provider_id: my-worker
model_id: my-model-v1
base_url: https://api.example.com/v1
display_name: My Model Worker
context_tokens: 128000
output_tokens: 8192
# runner: opencode          # optional, default opencode
# permissions: relaxed      # optional, reference a permission profile
# asset_prefix: my-model    # optional, default = key (legacy; no longer used for file naming)
# strengths: ...            # optional, surfaced by `pilot-workers status`
# suitable_modes: ...       # optional, surfaced by `pilot-workers status`
# notes: ...                # optional, surfaced by `pilot-workers status`
```

Then `pilot-workers credentials my-model` and `pilot-workers install claude` (or `codex`) to deploy/refresh the host playbook skill.

Reserved keys (cannot be used as provider key): `runner`, `all`, `on`, `claude`, `codex`.

## Host integration

The **host** is whatever AI agent acts as the planner. Each host ships ONE `pilot-workers` playbook skill (a doctrine playbook, not provider-specific syntax):

- **`claude-host/skills/pilot-workers/`**: playbook skill for Claude Code (installed to `~/.claude/skills/pilot-workers/`)
- **`codex-host/skills/pilot-workers/`**: the same playbook skill for Codex (installed to `$CODEX_HOME/skills/pilot-workers/`)

`pilot-workers install <host>` copies the skill into the host's skill directory; it carries no engine-specific knowledge and is the same regardless of which providers you've configured. (The v0.4.0 12-agents + 8-commands matrix is gone.)

Adding a new host: create `integrations/<name>-host/skills/pilot-workers/`, put whatever config your host needs, point it at `pilot-workers dispatch`. See `integrations/README.md`.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the current architecture, module reference, and conventions. See [docs/architecture.md](docs/architecture.md) for the detailed contract and security model.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest    # 297 tests, all offline
```

## License

MIT
