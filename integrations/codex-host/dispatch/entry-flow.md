# Shared Entry Flow for `$glm` and `$kimi`

Both short entries follow this exact flow. Per-entry specifics (provider argument,
credential command, display name) live in each entry's own `SKILL.md`; everything
here is common and must not be duplicated there.

## Keep the Planner in Control

- Honor an explicit `$glm` or `$kimi` invocation. Dispatch the selected provider even
  when Codex could complete the task itself; do not replace the user's provider choice
  with a cost or convenience heuristic.
- Keep requirements, design decisions, task decomposition, integration, and final
  acceptance with Codex. Give the worker only decisions that are already settled.
- Treat any additional worker review as a separate user decision. Never automatically
  add a second reviewer, switch providers, fan out review directions, or cross-review a
  code result. Codex's own diff inspection and verification remain mandatory and are
  not an additional worker review.
- Never silently fall back from one provider to the other.

## Parse the Call

Parse the first word after the entry name as the mode only when it is one of:
`code`, `explore`, `test`, `review`, `resume`.

Otherwise default to `code` and treat all following text as the task.

Use the remaining text as the task. If no task text remains, use the active
unresolved request from the current conversation. Do not ask the user to repeat
an already-known task. Ask one concise question only when no task can be recovered.

For `resume`, take the session ID from the remaining text or from the latest
`worker_runner.summary` of the same provider in the conversation. Reuse the work
directory reported by that same summary.

## Apply the Mode Contract

### `code`

Finish project inspection, requirements, planning, and applicable approval gates before
dispatch. Record the pre-run working-tree state so existing user changes are not
attributed to the worker. Give the worker one bounded implementation target with exact
paths and fast focused checks; keep the full behavioral verification for Codex after
the worker returns.

### `explore`

Delegate broad reading instead of duplicating it in Codex. State the questions and
directory scope explicitly, require every material conclusion to cite `file:line`, and
request no more than 20 primary conclusions unless the task needs another bound. After
the worker returns, spot-check the 2-3 most consequential claims. Widen verification
only when a spot-check fails or the evidence conflicts.

### `test`

Specify the exact commands, working directory, prerequisites, and known pre-existing
failures. The worker runs tests and collects statistics and original failures; it does
not diagnose by editing or fix failures. Afterward, confirm the reported command and
counts and check that the read-only run introduced no source changes. Codex owns failure
diagnosis and any later repair plan.

### `review`

Run a worker review only when the user asks for it. Preserve the provider, scope, review
directions, and desired parallelism chosen by the user; do not introduce an automatic
cross-model review. Require severity and `file:line` for each finding. Before including
a finding in the final answer, verify it against the current code and merge duplicates.

### `resume`

Resume only a prior `code` session with the same provider, session ID, and reported
workdir. Describe only the remaining gap and the required correction. Never use
`resume` to continue a read-only mode because it maps to the editable code agent. Do not
cold-start the same code task when a valid session exists. If the same obstacle survives
two visible attempts, stop and let Codex take over or report the blocker.

## Dispatch

1. Resolve the backend at `${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers`.
2. Read its `SKILL.md`, `references/task-spec.md`, and `references/provider-contract.md`.
   Preserve every isolation and security constraint.
3. Inspect only enough of the implicated project to satisfy the planner responsibilities
   and the selected mode contract. Complete any applicable requirements, TDD, approval,
   or task-contract gates before dispatch.
4. Build a self-contained task contract without secrets, following `references/task-spec.md`.
   The worker is an independent OpenCode process and cannot see the Codex conversation.
   Use exact paths and include all locked decisions, but do not repeat generic worker
   discipline already injected from `prompts/*.md`.
5. Run the backend synchronously with the entry's fixed provider argument:

   ```bash
   python3 <backend>/scripts/run_worker.py \
     --provider <PROVIDER_ARG> \
     --mode <mode> \
     --workdir <absolute-project-path> \
     --task-file <absolute-task-contract-path>
   ```

   The task contract travels to OpenCode via stdin, never argv. Keep the runner attached
   to the current Codex task; do not delegate runner ownership to another subagent.
6. Parse the first `worker_runner.started` event and report one concise update with the
   provider, model, mode, run ID, and rendered-log path. Continue polling the same runner
   process until it exits. Treat `worker_runner.heartbeat` as proof of life and summarize
   only useful progress, errors, and completion rather than replaying every raw event.
   Never leave a live worker behind and return control as if the task were finished.
7. Keep the user informed at least once per minute while a worker is running. For raw
   live progress, point them to
   `$CODEX_HOME/opencode-workers/logs/<provider>/latest.log` (`tail -f` it; lines carry a
   `|PID` tag, and `== 完成` / `!! ` mark completion and errors). Do not read that rendered
   log routinely when the attached stdout/stderr stream is intact; use the raw logs only
   to recover from truncated output, interruption, or a missing summary.
8. The runner enforces `--timeout` (default 3600 s) and `--idle-timeout` (default 900 s).
   Override them only for tasks with a known longer runtime.
9. For `resume`, add `--session <session-id>` and use the prior summary's work directory.
10. If credentials are missing, stop visibly and report the entry's credential command.

## Finish as the Planner

Parse the final `worker_runner.summary` line (session ID, workdir, exit code, log paths).
A nonzero exit, `timed_out`, `idle_timed_out`, or `interrupted` is a visible failure —
report it; never silently retry, and never fall back to the other provider.

After `code` or `resume`, compare the actual diff with both the task contract and the
pre-run working-tree state, inspect every changed production path, and run the smallest
real verification that would fail if the requested behavior were wrong. Do not launch an
extra worker review unless the user explicitly requests one.

For read-only modes, apply the corresponding mode-specific evidence check above. Never
mark the task complete solely because the worker says it is complete, and never present
an unverified material claim as Codex's conclusion.
