# Host Integrations

Each subdirectory contains config files for one **host** — the AI agent that acts as the planner/dispatcher.

```
integrations/
├── claude-host/     ← Claude Code (agents + slash commands)
├── codex-host/      ← Codex (skills + entry flow)
└── <your-host>/     ← add your own
```

## Adding a new host

Any AI agent that can:
1. Write a task contract (following `docs/task-spec.md`)
2. Call `python3 -m pilot_workers.cli.run --provider <key> --mode <mode> ...`
3. Parse `worker_runner.started` and `worker_runner.summary` from stdout

...can be a host. Create a directory here with whatever config format your host needs (skills, agents, commands, plugins, etc.) and point it at the runner CLI.

Current hosts use `--task-file` to pass the contract and `tail -f` on `latest.log` for live progress. The runner interface is the same regardless of host.

## Not a host?

If you're just adding a new **model provider** (not a new planner), you don't need anything here. Drop a YAML file in `providers/` instead.
