---
name: pilot-workers
description: Dispatch bounded tasks (code, explore, test, review, resume) to isolated LLM workers via the pilot-workers CLI and harvest their structured verdicts. Use whenever a task is worth delegating — bulk mechanical edits, codebase exploration, long test runs, multi-axis code review — so the main session keeps its context for planning and verification. Not for small tweaks, tasks needing mid-course judgment, or anything touching credentials/CI/production.
---

# pilot-workers Playbook

You are the planner. Workers are separate OpenCode processes that **cannot see
this conversation**. Planning, task decomposition, and final verification stay
with you; workers get only settled decisions.

## Quick Reference

```bash
pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md   # fill it in — self-contained
pilot-workers dispatch --provider <key> --mode <mode> --workdir "$PWD" --task-file <file>   # background Bash
```

- stdout is exactly two JSON lines: first `worker_runner.started` (record
  run_id and log paths), last `worker_runner.verdict`. **The completion signal
  = the background Bash exiting on its own.** Never poll by process name;
  never judge from the shared log (`latest.log` is for humans only).
- **Dispatch from the main session only, in a background Bash.** Subagents die
  at turn end and their background tasks are never harvested — never dispatch
  from a subagent, never "wait" by ending your turn.
- Consumption contract: read `verdict.result` first (the parsed PILOT_RESULT
  block). Read `final_text_path` at most once, only when
  `parse_state != "parsed"` or a finding needs surrounding context. Never
  auto-redispatch on parse failure alone.
- Resume-first recovery: on failure or non-convergence, resume with
  `--session <session_id>` + `--run-id <run_id>` from the original verdict
  before considering a cold redispatch.

## Verdict matrix

The `verdict` state string and the sibling flags `timed_out` / `idle_timed_out` /
`interrupted` are set independently — **check the flags even on `completed`**.

| state | meaning | action |
|---|---|---|
| `completed` | usable report (`parse_state` parsed, or long unstructured text) | consume `result` first; flags may still be set — say so |
| `step_capped_partial` | step cap hit | partial coverage; a parsed partial block is still salvaged into `result` — report the uncovered scope truthfully |
| `error` | summary-error or crash with no usable text | read `jsonl_path` for the post-mortem before concluding anything |
| `empty` | no usable output | read `jsonl_path`; report the cause of death truthfully |

Never draw a conclusion without reading the evidence. A `completed` verdict
with `timed_out: true` is a *timed-out* completion — report both facts.

Synthesized fanout verdicts carry `synthesized: true` + `reason`
(`crash|timeout|idle_timeout|interrupted`) + `stderr_tail`; `result` and
`final_text_path` are null. Consume rule: synthesized → read `reason` +
`stderr_tail` only.

## Task files

- The worker sees nothing but the task file: it must be **self-contained** —
  explicit file paths, completion criteria, do-not-touch boundaries. Never
  "that file" or "the module mentioned above".
- Unique `/tmp` names (`<provider>-<mode>-<slug>-<timestamp>.md`) so parallel
  sessions never clobber each other.
- **Never any credentials** in a task file.
- General discipline does not need to be written — dispatch injects
  `prompts/*.md` automatically, including the `PILOT_RESULT` output block the
  worker must end with.

## Mode: explore

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving.

- List what to investigate item by item; pin the scope to specific
  directories/file types to shrink roaming room.
- **Spot-check — do not parrot**: pick 2-3 of the most critical conclusions,
  open their cited `file:line`, and check. If they line up → trust the whole
  report; if not → the report is unreliable: rewrite the question and
  redispatch, or flag which conclusions are unverified. Conclusions without a
  `file:line` are untrusted outright.
- Bring conclusions back **with their file:line references, verbatim** — the
  main thread needs those references to plan; losing them ruins everything.
- Boundary: judgment/trade-off questions ("which approach is better") are your
  job, not the worker's. Mixed explore+change tasks: explore first, plan,
  then dispatch code.

## Mode: code

- **Worth-it self-check — push back if it is not worth it.** Is the expected
  change volume far larger than the task description? Worth it: bulk
  mechanical changes (renaming 50 files, scaffolding boilerplate, backfilling
  isomorphic tests), parallel fan-out, quota near the cap. Not worth it:
  small tweaks, mid-course judgment calls, specs about as long as the diff —
  a spec longer than the diff → don't dispatch; do it yourself.
- One worker call bundles at most 2-3 related fix points; split larger
  batches into multiple workers (they can run in parallel).
- **Parallel code jobs require `--worktree`** so each worker gets an isolated
  git worktree; one worker per background Bash; batch similar durations per
  fanout and split mixed durations. Clean up with
  `pilot-workers maintain worktrees remove <path>`.
- Gather intelligence, do not do deep verification (verified once, by you,
  not twice): `git diff --stat` for the change list, check for files outside
  the spec's boundaries, read `exit_code` / `session_id` / `steps` /
  `tool_errors` from the verdict.
- Then run the single verification pass yourself: `git diff --stat` against
  the spec whitelist, spot-check the actual diff, run tests/lint. Never treat
  the worker's completion claim as proof.
- **Cross-model review for rewrite-scale diffs** (hundreds of lines or more):
  dispatch a *different* provider in review mode before verifying — the two
  models' errors are uncorrelated, so the second catches the first's
  systematic blind spots at cheap quota. Skip for small changes.
- Do not edit a worker's target files while it is running. Deletion,
  migration, CI/keys/production config changes: do not dispatch; a human does
  those.

## Mode: test

- **Worth-it self-check — push back on fast suites.** Suite under 1 minute,
  or failure output expected short (a few dozen lines) → run it yourself;
  dispatch pays off for huge output (hundreds/thousands of failure lines to
  sift), repeated reruns, or when your context is near full.
- The task states the exact test command, directory, preconditions, and any
  known pre-existing failures. Run-and-gather only; fixes are your job.
- **Anti-false-positive**: counts + raw error text → trust it. "All passed"
  with no counts → suspicious → read `jsonl_path` to see what command
  actually ran, and redispatch if needed. Run `git status` to confirm the
  worker left no junk changes behind.
- Bring counts and the failure list (test name, raw error text, `file:line`)
  back verbatim; do not interpret root cause or propose fixes on the worker's
  behalf.

## Mode: review

- **Axis-splitting is your judgment call.** Fix 2-4 orthogonal axes
  (correctness/boundary conditions, security, performance, consistency with
  codebase conventions); one instance handles exactly one axis. Write each
  axis as its own self-contained task file.
- **Parallel fanout recipe**: one job per axis, launched together —

  ```bash
  pilot-workers fanout --job glm:review:/tmp/glm-review-correctness-<ts>.md \
                       --job kimi-k3:review:/tmp/kimi-review-security-<ts>.md
  ```

  (`--job PROVIDER:MODE:TASK_FILE`; mixing providers spreads load across
  quotas.) Each job collects its verdict independently.
- **Aggregation is your job.** Merge and dedupe findings across axes, sort by
  severity, report directly. Spot-check 1-2 high-severity findings at their
  cited `file:line` — false positives are the most common defect of review
  tasks; flag anything that does not check out as "verified false positive".
  Findings without a `file:line` are untrusted.
- Review mode cannot edit; fixes go through your planning — small fixes
  yourself, bulk mechanical fixes via a code dispatch.
- Axis too broad ("review the entire repo") → split axes first; unclear diff
  baseline → clarify the two versions being compared first.

## Mode: resume

- **Resume-first recovery.** When a run fails or does not converge, prefer
  resume — it reuses the full prior-session context and saves minute-scale
  round-trips versus a cold restart:

  ```bash
  pilot-workers dispatch --provider <key> --mode resume \
      --session <session_id from the verdict> --run-id <run_id from the verdict> \
      --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
  ```

  Take `--session` and `--run-id` from the original verdict. No template for
  resume — pass `--task` with what remains and how to fix it.
- **Two obstacles, then take over**: if the worker hits the same obstacle
  twice and still does not pass → the main session takes over and wraps up.
- The resume window == the sandbox retention window. A loud "session expired
  past retention" error means the sandbox was reaped — redispatch cold. A
  loud "run is still active" error means a live lock; do not force it.

## Providers

Minimal static table, **as of v0.5.0 — verify with `pilot-workers status`
when uncertain** (status shows each provider's `strengths`,
`suitable_modes`, and `notes` live from the YAML registry):

| key | strengths | suitable modes |
|---|---|---|
| `glm` | fast mechanical execution, cheap bulk edits and scaffolding | code, review |
| `kimi-k3` | large context, contract-grade precision | code, review, explore |
| `ds` | cheap high-volume reading; exploration and test harvesting | explore, test, review |

Cross-model review: never review a provider's rewrite-scale diff with the
same provider — errors would be correlated.

## Timeouts

- `--timeout <seconds>` bounds the whole job; the fanout parent watchdog
  enforces it plus a grace period.
- **`--timeout 0` forfeits the parent watchdog** for that job (no deadline,
  no silence kill); the child's `--idle-timeout` remains the only liveness
  bound.
- `--timeout 0 --idle-timeout 0` together make a job **fully unbounded** —
  avoid unless you are watching it yourself.
