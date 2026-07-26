---
name: pilot-workers
description: Plan with Codex, then run bounded tasks (code, explore, test, review, resume) through isolated LLM workers via the pilot-workers CLI and verify their structured verdicts. Invoke explicitly as `$pilot-workers [code|explore|test|review|resume] [task]`. Use whenever coding, investigation, testing, review or session continuation is worth handing to a worker model. {{PILOT_PROVIDER_TRIGGERS}} Not for small tweaks, mid-course judgment calls, or credentials/CI/production changes.
---

# pilot-workers Playbook

Parse the first word after `$pilot-workers` as the mode
(`code`/`explore`/`test`/`review`/`resume`); otherwise default to `code` and
treat all text as the task. Keep planning, task decomposition, and final
verification with Codex; give the worker only settled decisions. Workers are
separate OpenCode processes that **cannot see this conversation**.

## Quick Reference

0. **Choose `<key>` first.** Delegation happens for exactly two reasons: the
   user names a provider (that always wins), or the **Workers table** at the
   bottom of this file assigns one to your mode — it was generated for this host,
   so an exploration request needs no question asked. **A mode with no provider
   assigned is not an invitation to guess: do it yourself.** No assignment means
   the user never delegated that kind of work; picking a worker by its advertised
   strengths would delegate something they chose to keep. If a mode looks worth
   delegating, say so and let them run
   `pilot-workers install <provider> on <host> for <mode>`. Never dispatch to a
   provider absent from the table: it was not configured for this host.
1. `pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md`,
   then fill in the template (unique filename — parallel sessions must not
   collide). The task file must be **self-contained** and carry **never any
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
   `parse_state != "parsed"` or a finding requires surrounding context. Never
   auto-redispatch on parse failure alone.
5. Resume-first recovery: on failure or non-convergence, resume with
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

## Mode: explore

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving. List
what to investigate item by item; pin the scope to directories/file types.

- **Planning is the verification — do not sample.** Sampling a couple of
  conclusions proves nothing about the rest and manufactures confidence in it.
  Take the report into planning and let the gaps surface: a broad gap goes back
  as a rewritten explore dispatch, while three or five lines you still need —
  read them here, a round-trip costs more than the read.
- Conclusions carrying no `file:line` are untrusted: you cannot plan on them and
  cannot check them, so treat them as absent.
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
- Verify once, and verify what is cheap to verify COMPLETELY. `git diff --stat`
  plus `git status --short` settle one whole property cheaply: which files
  changed. Compare that set against the spec's whitelist — anything outside it
  is a boundary violation, and a stray untracked file is worth catching too.
  Reconcile the worker's own `FILES_CHANGED` against that set too — a
  mismatch means it misreported its work, which colours everything else it
  claimed. Also read `exit_code` / `session_id` / `steps` / `tool_errors`.
- **Do not sample the diff.** A few hunks of a large change license nothing
  about the rest. Correctness comes from the test run, from exercising the
  behaviour end to end, and — for rewrite-scale diffs — from the cross-model
  review below. Never treat the worker's completion claim as proof.
  **The test run goes to whichever provider is assigned to `test` in the Workers
  table**, as a `test`-mode dispatch; running it here would void that
  assignment. You still read the counts and failures and decide what they mean.
  Exception: a suite finishing in seconds with short output is cheaper to run
  here than to round-trip.
- **Cross-model review for rewrite-scale diffs** (several hundred lines or more):
  dispatch a *different* provider in review mode before verifying — the two
  models' errors are uncorrelated, so the second catches the first's
  systematic blind spots at cheap quota. Skip for small changes.
- Do not edit a worker's target files while it is running. Deletion,
  migration, CI/keys/production config: do not dispatch; a human does those.

## Mode: test

- **Worth-it self-check — push back on fast suites.** Suite under 1 minute,
  or short expected failure output → run it yourself. Dispatch pays off for
  huge output (a few hundred to several thousand failure lines), repeated
  reruns, or a near-full context.
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
  pilot-workers fanout --job <providerA>:review:/tmp/review-correctness-<ts>.md \
                       --job <providerB>:review:/tmp/review-security-<ts>.md
  ```

  (`--job PROVIDER:MODE:TASK_FILE`; mixing providers distributes load across
  quotas.) Each job collects its verdict independently.
- **Aggregation is your job.** Merge and dedupe across axes, sort by
  severity, report directly. **Verify every finding you intend to act on,
  before acting** — not a sample of them. A review finding is a claim you will
  answer by editing code, so a false positive turns into a wrong edit; open its
  cited `file:line` and confirm the defect is real. Findings you are NOT acting
  on need no verification: say they are unverified. Findings with no `file:line`
  are untrusted.
- **Try to REFUTE each high before acting on it, and refute its fix too.**
  Reviewers cannot run code (interpreters are denied in review mode), so their
  findings are readings — plausible and sometimes wrong. Run the thing: the input
  against the regex, the race with the lock disabled, the command in a scratch
  copy. A proposed fix is a separate claim and has been wrong even when the
  diagnosis was right. For a high you cannot settle by running something,
  dispatch a *different* provider in review mode to argue against it.
- Review mode cannot edit; small fixes yourself, bulk mechanical fixes via a
  code dispatch. Axis too broad → split first; unclear diff baseline →
  clarify the two versions first.

## Mode: resume

- **Resume-first recovery** — it reuses the full prior-session context and
  saves minute-scale round-trips versus a cold restart:

  ```bash
  pilot-workers dispatch --provider <key> --mode resume \
      --session <session_id from the verdict> --run-id <resume_run_id from the verdict> \
      --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
  ```

  Take `--session` from the verdict and `--run-id` from its **`resume_run_id`**,
  not its `run_id`: a resumed run gets a fresh `run_id` for its own log, so
  resuming twice off `run_id` names a sandbox that never existed and fails as
  "session expired". For a cold run the two are equal. No template —
  pass `--task` with what remains and how to fix it.
- **Two obstacles, then take over**: same obstacle twice and still not
  passing → Codex takes over and wraps up.
- The resume window == the sandbox retention window. A loud "session expired
  past retention" error means the sandbox was reaped — redispatch cold. A
  loud "run is still active" error means a live lock; do not force it.

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
