---
name: dispatch-opencode-workers
description: Internal shared backend for the short `$glm` and `$kimi` skills. Provides the fixed GLM 5.2 and Kimi K3 OpenCode runner, credential isolation, task-contract reference, logging, worktrees, and verification rules. Do not select this long skill directly for normal user requests; route user-facing GLM and Kimi work through `$glm` or `$kimi`.
---

# Dispatch OpenCode Workers

This is the internal shared backend. Normal user-facing invocation is `$glm <mode> [task]` or `$kimi <mode> [task]`.

Keep Codex responsible for requirements, planning, integration, and final verification. Give the worker a closed task contract, wait for it to finish, then inspect its changes and run the real verification yourself. Never treat a worker's completion statement as proof.

This skill permits only these fixed routes:

- `glm` -> `glm-worker/glm-5.2` at the official Zhipu Coding endpoint.
- `kimi-k3` -> `kimi-worker/k3` at the official Kimi Code endpoint.

Do not add a relay, arbitrary endpoint, arbitrary model, Claude runner, Claude session, or Claude configuration.

## Prepare Once

Resolve this skill's absolute directory and use its scripts from there.

1. Install the pinned, user-local runtime:

   ```bash
   bash <skill-dir>/scripts/install_runtime.sh
   ```

2. Check credential status without displaying a secret:

   ```bash
   python3 <skill-dir>/scripts/configure_credentials.py all --status
   ```

3. If a provider is missing, configure it in an interactive terminal:

   ```bash
   python3 <skill-dir>/scripts/configure_credentials.py glm
   python3 <skill-dir>/scripts/configure_credentials.py kimi-k3
   ```

The credential script writes each provider to a separate OpenCode data directory with mode `0600`. It never accepts a key on the command line and never prints one.

## Plan Before Dispatch

Finish all material decisions before invoking a worker. Read [references/task-spec.md](references/task-spec.md), then create a self-contained task file that states:

- the objective and observable completion boundaries;
- locked decisions and explicit non-goals;
- exact files or entry paths to inspect;
- allowed edit scope;
- verification commands and required final report.

Never place credentials, access tokens, private keys, cookies, or unrelated secrets in the task file. Dispatch only work whose source context may be sent to the selected official model provider.

## Select a Mode

- `code`: inspect and edit the scoped worktree, then run focused checks.
- `explore`: read-only investigation; no source edits.
- `test`: run validation without source edits; shell commands are restricted to common test, lint, build, and read-only Git commands.
- `review`: read-only review with evidence-backed findings.
- `resume`: continue a prior `code` session; requires its session ID and original work directory.

Honor an explicit provider choice. Without one, use `glm` for the first execution and `kimi-k3` when an independent second execution is useful. Never silently fall back from one provider to the other.

## Dispatch Synchronously

Run a planned task in the existing worktree:

```bash
python3 <skill-dir>/scripts/run_worker.py \
  --provider glm \
  --mode code \
  --workdir /absolute/project/path \
  --task-file /absolute/task.md
```

Use `--worktree` only when the repository is clean and the task should start from committed `HEAD`. The runner creates a detached worktree and reports its path; it deliberately leaves that worktree in place for Codex to inspect and integrate.

```bash
python3 <skill-dir>/scripts/run_worker.py \
  --provider kimi-k3 \
  --mode review \
  --workdir /absolute/project/path \
  --task-file /absolute/review-task.md \
  --worktree
```

The command blocks until OpenCode exits. It prints a `worker_runner.started` event, feeds the task contract via stdin (never argv), streams JSON events, emits JSON heartbeats to stderr during silent periods, writes provider-separated raw logs plus a rendered live log at `$CODEX_HOME/opencode-workers/logs/<provider>/latest.log` (`tail -f` friendly, `|PID`-tagged lines, `== 完成` / `!! ` markers), and finishes with a `worker_runner.summary` event. `--timeout` (default 3600 s) and `--idle-timeout` (default 900 s) terminate a stuck worker visibly; a nonzero exit, `timed_out`, `idle_timed_out`, or `interrupted` is a visible failure. Never retry silently and never fall back to the other provider.

Resume only with the session ID and work directory from that summary:

```bash
python3 <skill-dir>/scripts/run_worker.py \
  --provider glm \
  --mode resume \
  --session ses_example \
  --workdir /absolute/or/reported/worktree/path \
  --task "Address the two remaining failures and rerun the specified checks."
```

Use `--dry-run` to inspect the locked route, agent, isolation paths, and credential status without invoking a model or creating a worktree.

## Maintain

Logs and detached worktrees never disappear silently; clean them explicitly:

```bash
python3 <skill-dir>/scripts/maintain.py logs --older-than-days 14
python3 <skill-dir>/scripts/maintain.py worktrees list
python3 <skill-dir>/scripts/maintain.py worktrees remove /absolute/worktree/path
```

Log cleanup always keeps each provider's newest run. Worktree removal refuses dirty
worktrees and worktrees holding unintegrated commits; integrate first, then remove.

## Verify as Codex

After every `code` or `resume` run:

1. Inspect the actual diff and every changed production path.
2. Check that edits stayed within the task contract.
3. Run the smallest real verification that would fail if the requested behavior were wrong.
4. Integrate a detached worktree only after the review passes.
5. Report worker failures, incomplete boundaries, or unavailable credentials explicitly.

For `explore`, `test`, and `review`, verify the worker's claims against current files and command output before using them.

## Preserve Isolation

Read [references/provider-contract.md](references/provider-contract.md) before modifying provider or security settings.

- Keep OpenCode pinned to the version declared in the scripts.
- Keep sharing, auto-update, model-catalog fetching, external plugins, default plugins, Claude prompt loading, and Claude skill loading disabled.
- Keep each provider's XDG data, config, state, cache, session, and log directories separate.
- Keep the provider allowlist, exact model, exact endpoint, and agent permissions generated by the runner. Worker prompts live in `prompts/*.md`; the runner injects them as the agent system prompt.
- Do not export provider keys into the worker environment. OpenCode reads only the isolated local credential file and sends the key as authorization to the selected official provider.
- Do not use `--share`, a relay, or a Claude-owned runtime.
- Do not weaken `external_directory`, remote Git, network-shell, credential-file, or destructive-command denials to make a task pass. Return the blocked command to Codex for an explicit decision.
- Treat this as profile and policy isolation, not an operating-system sandbox. Do not dispatch hostile repositories or adversarial task text; use an OS/container sandbox or credential broker when the worker itself is outside the trust boundary.
