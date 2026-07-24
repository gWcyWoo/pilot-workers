---
name: pilot-workers
description: Plan with Codex, then run bounded tasks (code, explore, test, review, resume) through isolated LLM workers via the pilot-workers CLI and verify their structured verdicts. Invoke explicitly as `$pilot-workers [code|explore|test|review|resume] [task]`. Use whenever the user asks to delegate coding, investigation, testing, review, or session continuation to a worker model (glm, kimi-k3, ds). Not for small tweaks, mid-course judgment calls, or credentials/CI/production changes.
---

# pilot-workers Playbook

Parse the first word after `$pilot-workers` as the mode
(`code`/`explore`/`test`/`review`/`resume`); otherwise default to `code` and
treat all text as the task. Keep planning, task decomposition, and final
verification with Codex; give the worker only settled decisions. Workers are
separate OpenCode processes that **cannot see this conversation**.

## Quick Reference

1. `pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md`,
   then fill in the template (unique filename — parallel sessions must not
   collide). The task file must be **self-contained** and carry **never any
   credentials**. No template for `resume` — pass
   `--task "<what remains, how to fix>"` instead.
2. Run in a background shell **from the main session**:
   `pilot-workers dispatch --provider <key> --mode <mode> --workdir <absolute-project-path> --task-file <file>`.
   Subagents die at turn end and their background tasks are never harvested —
   never dispatch from a subagent, never end your turn to "wait".
3. stdout is exactly two JSON lines: first `worker_runner.started` (note
   run_id and log paths), last `worker_runner.verdict`. Completion signal =
   the background shell exiting on its own. Never poll by process name; never
   judge from shared logs (`latest.log` is for humans only). If the shell
   exits with fewer than two lines, check its stderr — early failures
   (missing credentials, runner not installed) produce no JSON.
4. Consumption contract: read `verdict.result` first (the parsed
   PILOT_RESULT block). Read `final_text_path` at most once, only when
   `parse_state != "parsed"` or a finding needs surrounding context. Never
   auto-redispatch on parse failure alone.
5. Resume-first recovery: on failure or non-convergence, resume with
   `--session <session_id>` + `--run-id <run_id>` from the original verdict
   before considering a cold redispatch. Missing credentials →
   `pilot-workers credentials <key>`.

## Verdict matrix

The `verdict` state string and the sibling flags `timed_out` / `idle_timed_out` /
`interrupted` are set independently — **check the flags even on `completed`**
(a `completed` verdict with `timed_out: true` is a timed-out completion;
report both facts).

- `completed` → usable report; consume `result` first.
- `step_capped_partial` → step cap hit; partial coverage. A parsed partial
  block is still salvaged into `result` — report the uncovered scope
  truthfully.
- `error` / `empty` → read `jsonl_path` for the post-mortem before concluding
  anything; report the cause of death truthfully. Never draw a conclusion
  without reading the evidence.
- Synthesized fanout verdicts (`synthesized: true`) carry `reason`
  (`crash|timeout|idle_timeout|interrupted`) + `stderr_tail`; `result` and
  `final_text_path` are null. Consume rule: read `reason` + `stderr_tail`
  only.

## Mode: explore

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving. List
what to investigate item by item; pin the scope to directories/file types.

- **Spot-check — do not parrot**: verify 2-3 of the most critical conclusions
  at their cited `file:line`. They line up → trust the report; they don't →
  rewrite and redispatch, or flag which conclusions are unverified.
  Conclusions without a `file:line` are untrusted outright.
- Bring conclusions back **with their file:line references, verbatim** —
  losing the references ruins the main thread's planning.
- Judgment/trade-off questions and file changes are your job; split mixed
  explore+change tasks: explore first, plan, then dispatch code.

## Mode: code

- **Worth-it self-check — push back if it is not worth it.** Is the expected
  change volume far larger than the task description? Worth it: bulk
  mechanical changes (renaming dozens of files, scaffolding boilerplate,
  backfilling tests), parallel fan-out, quota near the cap. Not worth it:
  small tweaks, mid-course judgment, specs about as long as the diff — a spec
  longer than the diff → don't dispatch; do it yourself.
- One worker call bundles at most 2-3 related fix points; split larger
  batches into multiple workers.
- **Parallel code jobs require `--worktree`** so each worker gets an isolated
  git worktree; one worker per background shell; batch similar durations per
  fanout and split mixed durations. Clean up with
  `pilot-workers maintain worktrees remove <path>`.
- Gather intelligence, not deep verification (verified once, by you):
  `git diff --stat` for the change list, check for files outside the spec's
  boundaries, read `exit_code` / `session_id` / `steps` / `tool_errors`.
  Then run the single verification pass yourself: diff against the spec
  whitelist, spot-check the actual diff, run tests/lint. Never treat the
  worker's completion claim as proof.
- **Cross-model review for rewrite-scale diffs** (hundreds of lines or more):
  dispatch a *different* provider in review mode before verifying — the two
  models' errors are uncorrelated, so the second catches the first's
  systematic blind spots at cheap quota. Skip for small changes.
- Do not edit a worker's target files while it is running. Deletion,
  migration, CI/keys/production config: do not dispatch; a human does those.

## Mode: test

- **Worth-it self-check — push back on fast suites.** Suite under 1 minute,
  or short expected failure output → run it yourself. Dispatch pays off for
  huge output (hundreds/thousands of failure lines), repeated reruns, or a
  near-full context.
- The task states the exact test command, directory, preconditions, and known
  pre-existing failures. Run-and-gather only; fixes are your job.
- **Anti-false-positive**: counts + raw error text → trust it. "All passed"
  with no counts → suspicious → read `jsonl_path` to see what command
  actually ran; redispatch if needed. Run `git status` to confirm the worker
  left no junk changes behind.
- Bring counts and the failure list (test name, raw error text, `file:line`)
  back verbatim; root-cause analysis is your job.

## Mode: review

- **Axis-splitting is your judgment call.** Fix 2-4 orthogonal axes
  (correctness/boundary conditions, security, performance, consistency);
  one instance handles exactly one axis, each in its own self-contained task
  file.
- **Parallel fanout recipe**: one job per axis, launched together —

  ```bash
  pilot-workers fanout --job glm:review:/tmp/glm-review-correctness-<ts>.md \
                       --job kimi-k3:review:/tmp/kimi-review-security-<ts>.md
  ```

  (`--job PROVIDER:MODE:TASK_FILE`; mixing providers spreads load across
  quotas.) Each job collects its verdict independently.
- **Aggregation is your job.** Merge and dedupe across axes, sort by
  severity, report directly. Spot-check 1-2 high-severity findings at their
  cited `file:line` — false positives are the most common review defect; flag
  failures as "verified false positive". Findings without a `file:line` are
  untrusted.
- Review mode cannot edit; small fixes yourself, bulk mechanical fixes via a
  code dispatch. Axis too broad → split first; unclear diff baseline →
  clarify the two versions first.

## Mode: resume

- **Resume-first recovery** — it reuses the full prior-session context and
  saves minute-scale round-trips versus a cold restart:

  ```bash
  pilot-workers dispatch --provider <key> --mode resume \
      --session <session_id from the verdict> --run-id <run_id from the verdict> \
      --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
  ```

  Take `--session` and `--run-id` from the original verdict; no template —
  pass `--task` with what remains and how to fix it.
- **Two obstacles, then take over**: same obstacle twice and still not
  passing → Codex takes over and wraps up.
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
