---
name: pilot-workers
description: Dispatch bounded tasks (code, explore, test, test-case, review, resume) to isolated LLM workers via the pilot-workers CLI and harvest their structured verdicts. {{PILOT_PROVIDER_TRIGGERS}}
---

# pilot-workers Playbook

You are the planner. Workers are separate OpenCode processes that **cannot see
this conversation**. Planning, task decomposition, and final verification stay
with you; workers get only settled decisions.

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

## Quick Reference

```bash
pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md   # fill it in — self-contained
pilot-workers dispatch --provider <key> --mode <mode> --workdir "$PWD" --task-file <file>   # background Bash
```

**Choose `<key>` by priority.** (1) Explicit user input always wins: a provider
the user names is used even where the Workers table assigns another, and an
explicit request to do the work here ("don't dispatch", "do it yourself") keeps
it in this session. (2) Otherwise the **Workers table** at the bottom of this
file decides: a routed mode is ALWAYS dispatched to its assigned provider —
never done in this session, and never weighed for whether it is worth a worker.
`install ... for <mode>` was the user making that economics decision once; it
is not yours to remake per task. If a routed dispatch seems wasteful, dispatch
anyway and say so afterwards — the user can
`pilot-workers uninstall for <mode> on <host>`. (3) **A mode with no provider
assigned is not an invitation to guess: do it yourself, in this session.** No
assignment means the user never delegated that kind of work; picking a worker
by its advertised strengths would delegate something they chose to keep. If a
mode looks worth delegating, say so and let them run
`pilot-workers install <provider> on <host> for <mode>`. Never dispatch to a
provider absent from the table: it was not configured here.

**Then Read `modes/<mode>.md`** (next to this file in the skill directory)
before writing the task file — it is that mode's task recipe and verification
contract. Never dispatch a mode whose playbook you have not read in this
session.

- stdout is exactly two JSON lines: first `worker_runner.started` (record
  run_id and log paths), last `worker_runner.verdict`. **The completion signal
  = the background Bash exiting on its own.** Never poll by process name;
  never judge from the shared log (`latest.log` is for humans only). Fewer than
  two lines means the run died before starting — read its stderr, because an
  early failure (missing credential, runner not installed) emits no JSON at all.
- **Dispatch from the main session only, in a background Bash.** Subagents die
  at turn end and their background tasks are never harvested — never dispatch
  from a subagent, never "wait" by ending your turn.
- Consumption contract: read `verdict.result` first (the parsed PILOT_RESULT
  block). Read `final_text_path` at most once, only when
  `parse_state != "parsed"` or a finding requires surrounding context. Never
  auto-redispatch on parse failure alone.
- Resume-first recovery: on failure or non-convergence, resume with
  `--session <session_id>` + `--run-id <resume_run_id>` from the original verdict
  before considering a cold redispatch. A `credential missing` error is not a
  worker failure and resume will not fix it — the provider has no API key yet:
  tell the user to run
  `pilot-workers install <provider> on <host> --global-key`, which prompts for
  it. The key belongs to the provider, so it is configured once for every host.
  Never handle a key yourself.

## Verdict matrix

The `verdict` state string and the sibling flags `timed_out` / `idle_timed_out` /
`interrupted` are set independently — **check the flags even on `completed`**.

| state | meaning | action |
|---|---|---|
| `completed` | usable report (`parse_state` parsed, or long unstructured text) | consume `result` first; flags may still be set — say so |
| `step_capped_partial` | step cap hit | partial coverage; a parsed partial block is still salvaged into `result` — report the uncovered scope truthfully |
| `error` | summary-error or crash with no usable text | `stderr_tail` first; open `jsonl_path` only if it does not explain the death |
| `empty` | no usable output | same: `stderr_tail`, then `jsonl_path` if needed |

Never draw a conclusion without reading the evidence.

`duration_s` / `tokens` / `final_text_len` are for your next decision, not the
report: cost so far, and whether `final_text_path` is worth opening at all
(`parse_state: parsed` means `result` already has everything).

`parse_state` values: `parsed` (consume `result`), `malformed` (the worker DID
write a result block and it did not parse — the work is real, read
`final_text_path`), `unstructured` (a block that was cut off mid-write — treat as
partial), `unavailable` (no block at all — the worker ignored the contract).

Synthesized fanout verdicts carry `synthesized: true` + `reason`
(`crash|timeout|idle_timeout|interrupted`) + `stderr_tail`; `result` and
`final_text_path` are null. Consume rule: synthesized → read `reason` +
`stderr_tail` only.

## Task files

- **Every task file must settle three things before dispatch:**
  1. **Objective** — what the worker must deliver, stated as verifiable outcomes.
  2. **Interface and steps** — the entry points (file:line), conventions to
     follow, and the approach to take. The worker must not guess these.
  3. **Boundaries** — what the worker must NOT do: files it must not touch,
     patterns it must not use, scope it must not exceed. An unlisted boundary
     is an invisible one.
- The worker sees nothing but the task file: it must be **self-contained** —
  explicit file paths, completion criteria, do-not-touch boundaries. Never
  "that file" or "the module mentioned above".
- Unique `/tmp` names (`<provider>-<mode>-<slug>-<timestamp>.md`) so parallel
  sessions never clobber each other.
- **Never any credentials** in a task file — it is sent verbatim to a
  third-party endpoint. Dispatch refuses obvious key shapes and any key
  configured on this machine, but that check **cannot catch every secret**:
  it is a backstop, not permission to paste config. Reference a value by
  env var and let the worker read it.
- **The worker can see the whole workdir.** Opening an `auth.json` or `.env` is
  denied for both the shell and the read tool, but a path deny cannot stop a
  recursive content search from returning a line out of one. If the project holds
  live secrets in untracked files, dispatch with `--worktree`: a detached
  worktree materialises tracked files only, so a gitignored `.env` is not there
  at all — a boundary rather than a pattern.
- General discipline does not need to be written — dispatch injects
  `prompts/*.md` automatically, including the `PILOT_RESULT` output block the
  worker must end with.

## Parallel test-case + code workflow

When both test-case and code are routed, run them in parallel and merge:

1. **Dispatch both simultaneously** — test-case with `--worktree` (isolated
   copy), code on the main workdir. Two background Bash shells, both started
   in the same turn.
   ```bash
   pilot-workers dispatch --provider <tc-key> --mode test-case --workdir "$PWD" --task-file <tc-task> --worktree &
   pilot-workers dispatch --provider <code-key> --mode code --workdir "$PWD" --task-file <code-task> &
   ```
2. **Wait for both to complete.** Harvest each verdict independently.
3. **Merge test-case into main.** The worktree path is in the
   `worker_runner.started` JSON. Test files are new files; source changes are
   in main — conflicts are rare.
   ```bash
   cd <worktree-path> && git add -A && git commit -m "test-case: <summary>"
   cd "$PWD" && git merge <worktree-branch>   # or cherry-pick
   ```
4. **Dispatch test** on the merged tree — the tests were written by
   test-case, the code was written by code, now verify them together.
5. **Clean up** once test passes:
   ```bash
   pilot-workers maintain worktrees remove <worktree-path>
   ```
   If test fails, fix in the main session and re-run — the failure tells you
   whether the interface changed (code's fault) or the test is wrong
   (test-case's fault).

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
