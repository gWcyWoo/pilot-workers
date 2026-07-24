# pilot-workers

Dispatch bounded tasks to isolated LLM workers. Your main AI agent (Claude, Codex, or any planner) stays in control of requirements, planning, and verification — the worker only executes what it's told.

## What it does

- **Provider isolation**: each model (GLM, Kimi, DeepSeek, or your own) gets its own credentials, XDG directories, logs, and session storage. No cross-contamination.
- **Fixed routing**: provider, model, and endpoint are locked per YAML config. Tasks cannot override them.
- **Security by default**: API keys never appear in CLI args, environment variables, task contracts, or logs. Output is auto-redacted.
- **Five modes**: `code` (edit), `explore` (read-only), `test` (run tests), `review` (read-only audit), `resume` (continue a prior code session).
- **Pluggable runners**: the runner adapter layer (`Runner` ABC) abstracts engine-specific details. Currently ships with OpenCode; designed for future alternatives.
- **Observable**: two-line JSON contract (`started` + `verdict`) for AI planners; human-readable `latest.log` for `tail -f`.
- **Per-provider installs**: `install glm on claude` deploys only GLM's integration to Claude Code. Mix and match providers × hosts.

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

# 3. Deploy integrations to your host
pilot-workers install all on claude    # Claude Code: 12 agents + 8 slash commands
pilot-workers install all on codex     # Codex: $glm / $kimi / $ds skills
pilot-workers install glm on claude    # or just one provider on one host

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
  install          Install integrations or runner.
                     install <provider|all> on <host|all>
                     install runner <name>
  uninstall        Remove integrations or runner.
                     uninstall <provider|all> on <host|all>
                     uninstall runner <name>
  status           Show provider credentials, host installs, and runner state.
                     status [--json]
                     status <provider> on <host>
  credentials      Configure isolated worker credentials.
  maintain         Worker log and worktree lifecycle tools.
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
# asset_prefix: my-model    # optional, default = key; used for integration file naming
```

Then `pilot-workers credentials my-model` and `pilot-workers install my-model on claude`.

Reserved keys (cannot be used as provider key): `runner`, `all`, `on`, `claude`, `codex`.

## Host integration

The **host** is whatever AI agent acts as the planner. `integrations/` has ready-made configs:
- **`claude-host/`**: 12 agents (glm/kimi/ds × coder/explorer/reviewer/tester) + 8 slash commands
- **`codex-host/`**: `$glm` / `$kimi` / `$ds` skill entry points

Adding a new host: create `integrations/<name>-host/`, put whatever config your host needs, point it at `pilot-workers dispatch`. See `integrations/README.md`.

## Architecture

See [CLAUDE.md](CLAUDE.md) for the current architecture, module reference, and conventions. See [docs/architecture.md](docs/architecture.md) for the detailed contract and security model.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest    # 130 tests, all offline
```

## License

MIT
