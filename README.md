# pilot-workers

Dispatch bounded tasks to isolated LLM workers. Your main AI agent (Claude, Codex, or any planner) stays in control of requirements, planning, and verification — the worker only executes what it's told.

## What it does

- **Provider isolation**: each model (GLM, Kimi, or your own) gets its own credentials, XDG directories, logs, and session storage. No cross-contamination.
- **Fixed routing**: provider, model, and endpoint are locked per YAML config. Tasks cannot override them.
- **Security by default**: API keys never appear in CLI args, environment variables, task contracts, or logs. Output is auto-redacted.
- **Five modes**: `code` (edit), `explore` (read-only), `test` (run tests), `review` (read-only audit), `resume` (continue a prior code session).
- **Observable**: `worker_runner.started` → heartbeats → `worker_runner.summary`. Human-readable live log at `latest.log` for `tail -f`.
- **Pluggable runners**: currently wraps [OpenCode](https://opencode.ai) via `@ai-sdk/openai-compatible`; the runner interface is designed for future alternatives (Aider, Continue, etc.).

## Quick start

```bash
# 1. Install the pinned OpenCode runtime
bash scripts/install_runtime.sh

# 2. Add a provider (YAML files in providers/)
# GLM and Kimi are included; add your own by copying the template

# 3. Configure credentials (interactive, key never displayed)
python3 -m pilot_workers.credentials glm
python3 -m pilot_workers.credentials kimi-k3

# 4. Verify
python3 -m pilot_workers.cli.run --provider glm --mode explore --workdir . --task "hello" --dry-run

# 5. Run a real task
python3 -m pilot_workers.cli.run \
  --provider glm --mode code \
  --workdir /path/to/project \
  --task-file /path/to/task.md
```

## Adding a new provider

Drop a YAML file in `providers/`:

```yaml
key: deepseek
provider_id: deepseek-worker
model_id: deepseek-coder-v3
base_url: https://api.deepseek.com/v1
display_name: DeepSeek Coder V3
context_tokens: 128000
output_tokens: 8192
```

Then `python3 -m pilot_workers.credentials deepseek` and you're done.

## Host integration

The **host** is whatever AI agent acts as the planner — Claude, Codex, Gemini, GLM itself, or anything that can write a task file and call the CLI.

`integrations/` has ready-made configs for current hosts:
- **`claude-host/`**: 8 agents + 8 slash commands (`/glm:code`, `/kimi:explore`, etc.)
- **`codex-host/`**: `$glm` / `$kimi` skill entry points

Adding a new host: create `integrations/<name>-host/`, put whatever config your host needs, point it at `python3 -m pilot_workers.cli.run`. See `integrations/README.md`.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the shared contract, security model, and verification checklist.

## License

MIT
