# Host Integrations

Each subdirectory contains config files for one **host** — the AI agent that acts as the planner/dispatcher.

```
integrations/
├── claude-host/     ← Claude Code (agents + slash commands)
├── codex-host/      ← Codex (skills)
└── <your-host>/     ← add your own
```

## Adding a new host

Any AI agent that can:
1. Generate and fill a task template: `pilot-workers template <mode>`
2. Call `pilot-workers dispatch --provider <key> --mode <mode> --workdir <path> --task-file <file>`
3. Parse the two-line stdout contract from `pilot-workers dispatch`: `worker_runner.started` + `worker_runner.verdict`

...can be a host. Create a directory here with whatever config format your host needs (skills, agents, commands, plugins, etc.) and point it at the runner CLI.

Current hosts use `--task-file` to pass the contract and `tail -f` on `latest.log` for live progress. The runner interface is the same regardless of host.

## Not a host?

If you're just adding a new **model provider** (not a new planner), you don't need anything here. Drop a YAML file in `data/providers/` instead.
