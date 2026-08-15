# pilot-workers

Dispatch bounded tasks to isolated LLM workers. Your main AI agent (Claude, Codex, or any planner) stays in control of requirements, planning, and verification — the worker only executes what it's told.

CLI command: `pw9` (alias: `pilot-workers`).

## What it does

- **Provider isolation**: each model (GLM, Kimi, DeepSeek, or your own) gets its own credentials, XDG directories, logs, and session storage. No cross-contamination.
- **Fixed routing**: provider, model, and endpoint are locked per YAML config. Tasks cannot override them.
- **Security by default**: API keys never appear in CLI args, environment variables, task contracts, or logs. Output is auto-redacted.
- **Five modes**: `code` (edit), `explore` (read-only), `test` (run tests), `review` (read-only audit), `resume` (continue a prior code session).
- **Auto-fanout review/test**: `pw9 review` and `pw9 test` automatically split work by configurable axes/layers and dispatch in parallel.
- **Pluggable runners**: the runner adapter layer (`Runner` ABC) abstracts engine-specific details. Currently ships with OpenCode.
- **Observable**: two-line JSON contract (`started` + structured `verdict`) for AI planners; human-readable `latest.log` for `tail -f`.

## Install

```bash
pip install pilot-workers
```

## Quick Start

```bash
# 1. Install the worker runtime
pw9 install runner opencode

# 2. Configure a provider's API key
pw9 key glm

# 3. Check status
pw9 status

# 4. Run a task (dispatch = run + a structured verdict and a report.md)
pw9 template code > /tmp/task.md   # fill it in first
pw9 dispatch --provider glm --mode code --workdir /path/to/project --task-file /tmp/task.md

# 5. Auto-fanout code review (5 axes in parallel)
pw9 review --provider glm --workdir .

# 6. Auto-fanout test execution
pw9 test --provider glm --workdir .
```

## Adding a Provider

Drop a YAML file in `data/providers/` with the required fields (`key`, `provider_id`, `model_id`, `base_url`, `display_name`, `context_tokens`, `output_tokens`), then `pw9 key <key>`.

## Key Commands

| Command | Description |
|---------|-------------|
| `pw9 key <provider>` | Configure API key |
| `pw9 status [--json]` | Provider credentials + runner state |
| `pw9 dispatch --provider <key> --mode <mode> --workdir <dir> --task-file <file>` | Dispatch a single task; stdout = two JSON lines (started + verdict), plus a report.md on disk |
| `pw9 run ...` | The inner primitive `dispatch` wraps — streams events, writes the log, but extracts no result |
| `pw9 spec --provider <key> --workdir <dir> --requirement "..."` | Explore, then draft a code task contract to edit |
| `pw9 discuss --provider <a>,<b> --workdir <dir> --question "..."` | One question, independent positions, shows where they split |
| `pw9 runs` / `pw9 usage` | Dispatch history and token totals |
| `pw9 fanout --workdir <dir> --job <provider:mode:file> ...` | Dispatch several jobs concurrently |
| `pw9 review --provider <key> --workdir <dir>` | Auto-fanout code review by axes |
| `pw9 test --provider <key> --workdir <dir>` | Auto-fanout test execution by layers |
| `pw9 review show/add/edit/remove` | Manage review axes |
| `pw9 test show/add/edit/remove` | Manage test layers |
| `pw9 permissions add/remove/show <provider> ...` | Per-provider shell permission overrides |
| `pw9 template <mode>` | Print the task template for a mode |
| `pw9 install runner opencode` | Install the worker runtime |
| `pw9 uninstall key <provider>` | Remove an API key |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## License

MIT
