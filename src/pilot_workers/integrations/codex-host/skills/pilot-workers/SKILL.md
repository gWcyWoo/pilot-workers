---
name: pilot-workers
description: Executor layer — assigns each kind of work (code, explore, test, test-case, review, resume) to an isolated LLM worker (provider) without changing the main session's own workflow, task decomposition, or verification strategy. When the main session decides to write code, write test cases, run tests, explore, or review, the assigned provider executes it. Invoke explicitly as `$pilot-workers [code|explore|test|test-case|review|resume] [task]`. {{PILOT_PROVIDER_TRIGGERS}}
---

# pilot-workers Playbook

Parse the first word after `$pilot-workers` as the mode
(`code`/`explore`/`test`/`test-case`/`review`/`resume`); otherwise default to
`code` and treat all text as the task. Keep planning, task decomposition,
workflow decisions (TDD or not, what order, how to verify), and final
verification with Codex — pilot-workers only assigns WHO executes each kind of
work, never WHAT to do or in WHAT order. Workers are separate OpenCode
processes that **cannot see this conversation**.

## When this skill applies

Two kinds of moments route here; match either, then choose the provider by
the priority chain below.

| Moment | Mode |
| --- | --- |
| "how does X work" / "trace this flow" / "探索、梳理、理解、读一下这块代码" — or YOU need to read code to build understanding before planning | explore |
| "implement/fix/refactor X" / "改一下、实现、修复" — or the plan is settled and edits would begin | code |
| "run the tests" / "跑一下测试" — or you want a suite run to verify a change | test |
| "generate tests for X" / "写测试用例" — or you need test cases written for a module | test-case |
| "review this change" / "review 一下这次改动" — or a rewrite-scale diff just landed | review |
| a prior dispatch failed or stopped short | resume |

The self-initiated rows draw the line by PURPOSE, never by size: reading to
LEARN (trace a flow, understand a module, gather context for a change) is an
explore dispatch no matter how few files; verifying a KNOWN location (a cited
file:line, a returned diff, a verdict) is local reading and needs no worker.

**test-case routing classification.** A request that bundles production code +
tests ("implement X with tests") routes to `code` — the tests will pass after
the code change, which violates test-case's ALL RED contract. `test-case` is
standalone batch test generation against behavior that does not exist yet. One
dispatch covers a cohesive unit; never dispatch test-case once per individual
test — that is vertical-slice TDD and stays in this session.

## Quick Reference

0. **Choose `<key>` by priority.** (1) Explicit user input always wins: a
   provider the user names is used even where the Workers table assigns
   another, and an explicit request to do the work here keeps it here.
   (2) Otherwise the **Workers table** at the bottom of this file decides: a
   routed mode is ALWAYS dispatched to its assigned provider — never done in
   this session, and never weighed for whether it is worth a worker.
   `install ... for <mode>` was the user making that economics decision once;
   it is not yours to remake per task. If a routed dispatch seems wasteful,
   dispatch anyway and say so afterwards — the user can
   `pilot-workers uninstall for <mode> on <host>`. (3) **A mode with no
   provider assigned is not an invitation to guess: do it yourself.** No
   assignment means the user never delegated that kind of work; picking a
   worker by its advertised strengths would delegate something they chose to
   keep. If a mode looks worth delegating, say so and let them run
   `pilot-workers install <provider> on <host> for <mode>`. Never dispatch to
   a provider absent from the table: it was not configured for this host.
1. Read `modes/<mode>.md` (next to this file in the skill directory) — that
   mode's task recipe and verification contract. Never dispatch a mode whose
   playbook you have not read in this session.
2. `pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md`,
   then fill in the template (unique filename — parallel sessions must not
   collide). **Every task file must settle three things before dispatch:**
   (a) **Objective** — what the worker must deliver, stated as verifiable
   outcomes. (b) **Interface and steps** — the entry points (file:line),
   conventions to follow, and the approach to take; the worker must not
   guess these. (c) **Boundaries** — what the worker must NOT do: files it
   must not touch, patterns it must not use, scope it must not exceed; an
   unlisted boundary is an invisible one.
   The task file must be **self-contained** and carry **never any
   credentials** — it is sent verbatim to a third-party endpoint. Dispatch
   refuses obvious key shapes and any key configured on this machine, but that
   check **cannot catch every secret**: it is a backstop, not permission to paste
   config. Reference a value by env var and let the worker read it.
   **The worker can also see the whole workdir.** Opening an `auth.json` or
   `.env` is denied for both the shell and the read tool, but a path deny cannot
   stop a recursive content search from returning a line out of one. If the
   project holds live secrets in untracked files, dispatch with `--worktree`: a
   detached worktree materialises tracked files only, so a gitignored `.env` is
   not there at all — a boundary rather than a pattern.
   No template for `resume` — pass
   `--task "<what remains, how to fix>"` instead.
3. Run in a background shell **from the main session**:
   `pilot-workers dispatch --provider <key> --mode <mode> --workdir <absolute-project-path> --task-file <file>`.
   Subagents die at turn end and their background tasks are never harvested —
   never dispatch from a subagent, never end your turn to "wait".
4. stdout is exactly two JSON lines: first `worker_runner.started` (note
   run_id and log paths), last `worker_runner.verdict`. Completion signal =
   the background shell exiting on its own. Never poll by process name; never
   judge from shared logs (`latest.log` is for humans only). If the shell
   exits with fewer than two lines, check its stderr — early failures
   (missing credentials, runner not installed) produce no JSON.
5. Consumption contract: read `verdict.result` first (the parsed
   PILOT_RESULT block). Read `final_text_path` at most once, only when
   `parse_state != "parsed"` or a finding requires surrounding context. Never
   auto-redispatch on parse failure alone.
6. Resume-first recovery: on failure or non-convergence, resume with
   `--session <session_id>` + `--run-id <resume_run_id>` from the original verdict
   before considering a cold redispatch. Missing credentials →
   `pilot-workers install <key> on <host> --global-key`.

## Verdict matrix

The `verdict` state string and the sibling flags `timed_out` / `idle_timed_out` /
`interrupted` are set independently — **check the flags even on `completed`**
(a `completed` verdict with `timed_out: true` is a timed-out completion;
report both facts).

- `completed` → usable report; consume `result` first.
- `step_capped_partial` → step cap hit; partial coverage. A parsed partial
  block is still salvaged into `result` — report the uncovered scope
  truthfully.
- `error` / `empty` → read `stderr_tail` first, then `jsonl_path` if it does not
  explain the death; report the cause truthfully. Never draw a conclusion
  without reading the evidence.
- `duration_s` / `tokens` / `final_text_len` inform your next decision, not the
  report: cost so far, and whether `final_text_path` is worth opening at all
  (`parse_state: parsed` means `result` already has everything).

`parse_state` values: `parsed` (consume `result`), `malformed` (the worker DID
write a result block and it did not parse — the work is real, read
`final_text_path`), `unstructured` (a block that was cut off mid-write — treat as
partial), `unavailable` (no block at all — the worker ignored the contract).
- Synthesized fanout verdicts (`synthesized: true`) carry `reason`
  (`crash|timeout|idle_timeout|interrupted`) + `stderr_tail`; `result` and
  `final_text_path` are null. Consume rule: read `reason` + `stderr_tail`
  only.

## Dispatching test-case and code together

When both test-case and code are routed, dispatch both in parallel.
The main session decides WHETHER to use test-case; once both are needed,
the parallel mechanics below apply.

1. **Dispatch both simultaneously** — test-case with `--worktree` (isolated
   copy), code on the main workdir. Two background shells, same turn.
   ```bash
   pilot-workers dispatch --provider <tc-key> --mode test-case --workdir "$PWD" --task-file <tc-task> --worktree &
   pilot-workers dispatch --provider <code-key> --mode code --workdir "$PWD" --task-file <code-task> &
   ```
2. **Contracts**: test-case must be ALL RED (every test fails — testing
   behavior that does not exist yet); code must be ALL GREEN (regression).
3. **Merge depends on who finishes first:**
   - **test-case first** → merge test files immediately. Code is still
     running; its validation will discover the new tests and becomes both
     regression AND new-test verification.
   - **code first** → code's validation is regression-only. Wait for
     test-case, then merge test files.
4. **Main session validates after merge** — run the project's test command
   locally. This is the authoritative result.
   ```bash
   cd <worktree-path> && git add -A && git commit -m "test-case: <summary>"
   cd "$PWD" && git merge <worktree-branch>   # or cherry-pick
   ```
- **Clean up** the worktree when done:
  ```bash
  pilot-workers maintain worktrees remove <worktree-path>
  ```

<!--PILOT_GENERATED_BEGIN-->
<!--PILOT_GENERATED_END-->

## Providers

Provider strengths and suitable modes live in the provider YAML registry,
not here — run `pilot-workers status <host>` when you need them (status
renders each provider's `strengths`, `suitable_modes`, and `notes` live).

## Timeouts

- `--timeout <sec>` caps the whole job; the fanout parent watchdog
  enforces it plus a grace period.
- **`--timeout 0` forfeits the parent watchdog** for that job (no deadline,
  no silence kill); the child's `--idle-timeout` remains the only liveness
  bound.
- `--timeout 0 --idle-timeout 0` together make a job **fully unbounded** —
  avoid unless you are watching it yourself.
