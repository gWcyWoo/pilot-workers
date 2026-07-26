---
name: pilot-workers
description: Dispatch bounded tasks (code, explore, test, review, resume) to isolated LLM workers via the pilot-workers CLI and harvest their structured verdicts. Use whenever a task is worth delegating — bulk mechanical edits, codebase exploration, long test runs, multi-axis code review — so the main session keeps its context for planning and verification. Not for small tweaks, tasks needing mid-course judgment, or anything touching credentials/CI/production. {{PILOT_PROVIDER_TRIGGERS}}
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

**Choose `<key>` first.** Delegation happens for exactly two reasons: the user
names a provider (that always wins), or the **Workers table** at the bottom of
this file assigns one to your mode — it was generated for this host, so "explore
the upload flow" needs no question asked. **A mode with no provider assigned is
not an invitation to guess: do it yourself, in this session.** No assignment
means the user never delegated that kind of work; picking a worker by its
advertised strengths would delegate something they chose to keep. If a mode looks
worth delegating, say so and let them run
`pilot-workers install <provider> on <host> for <mode>`. Never dispatch to a
provider absent from the table: it was not configured here.

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

## Mode: explore

Reading code is the bulk of token spend (read-to-write ratio roughly 50:1), so
exploration is a top dispatch candidate. Write the question straight into the
task file; do not read the code yourself first — that wastes the saving.

- List what to investigate item by item; pin the scope to specific
  directories/file types to shrink roaming room.
- **Planning is the verification — do not sample.** Sampling a couple of
  conclusions proves nothing about the rest and manufactures confidence in it.
  Instead take the report into planning; the gaps surface as you use it:
  a broad gap (wrong subsystem, missing the leads you need) goes back as a
  rewritten explore dispatch, while three or five lines you still need — read
  them here, a round-trip costs more than the read.
- Conclusions carrying no `file:line` are untrusted: you cannot plan on them and
  cannot check them, so treat them as absent.
- Bring conclusions back **with their file:line references, verbatim** — the
  main thread requires those references to plan; losing them ruins everything.
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
- Verify once, and verify what is cheap to verify COMPLETELY. `git diff --stat`
  plus `git status --short` settle one whole property for a few hundred tokens:
  which files changed. Compare that set against the spec's whitelist — anything
  outside it is a boundary violation, and a stray untracked file is worth
  catching too. Reconcile the worker's own `FILES_CHANGED` against that set too — a
  mismatch means it misreported its work, which colours everything else it
  claimed. Also read `exit_code` / `session_id` / `steps` / `tool_errors`
  from the verdict.
- **Do not sample the diff.** Reading a few hunks of a large change licenses
  nothing about the rest; it costs attention and buys confidence you have not
  earned. Correctness comes from the test run, from exercising the behaviour
  end to end, and — for rewrite-scale diffs — from the cross-model review below.
  Never treat the worker's completion claim as proof. **The test run goes to whichever provider is
  assigned to `test` in the Workers table**, as a `test`-mode dispatch; running
  it here would void that assignment. You still read the counts and failures and
  decide what they mean. Exception: a suite that finishes in seconds with short
  output is cheaper to run here than to round-trip — see the test-mode
  worth-it check.
- **Cross-model review for rewrite-scale diffs** (several hundred lines or more):
  dispatch a *different* provider in review mode before verifying — the two
  models' errors are uncorrelated, so the second catches the first's
  systematic blind spots at cheap quota. Skip for small changes.
- Do not edit a worker's target files while it is running. Deletion,
  migration, CI/keys/production config changes: do not dispatch; a human does
  those.

## Mode: test

- **Worth-it self-check — push back on fast suites.** Suite under 1 minute,
  or failure output expected short (a few dozen lines) → run it yourself;
  dispatch pays off for huge output (a few hundred to several thousand
  failure lines to sift), repeated reruns, or when your context is near full.
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
  pilot-workers fanout --job <providerA>:review:/tmp/review-correctness-<ts>.md \
                       --job <providerB>:review:/tmp/review-security-<ts>.md
  ```

  (`--job PROVIDER:MODE:TASK_FILE`; mixing providers distributes load across
  quotas.) Each job collects its verdict independently.
- **Aggregation is your job.** Merge and dedupe findings across axes, sort by
  severity, report directly. **Verify every finding you intend to act on,
  before acting** — not a sample of them. A review finding is a claim you will
  answer by editing code, so a false positive turns into a wrong edit; open its
  cited `file:line` and confirm the defect is real. Findings you are NOT acting
  on need no verification: say they are unverified. Findings with no `file:line`
  are untrusted.
- **Try to REFUTE each high before acting on it, and refute its fix too.**
  Reviewers cannot run code (interpreters are denied in review mode), so their
  findings are readings — plausible and sometimes wrong. Run the thing: the
  input against the regex, the race with the lock disabled, the command in a
  scratch copy. A proposed fix is a separate claim and has been wrong even when
  the diagnosis was right. For a high you cannot settle by running something,
  dispatch a *different* provider in review mode to argue against it.
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
      --session <session_id from the verdict> --run-id <resume_run_id from the verdict> \
      --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
  ```

  Take `--session` from the verdict and `--run-id` from its **`resume_run_id`**,
  not its `run_id`: a resumed run gets a fresh `run_id` for its own log, so
  resuming twice off `run_id` names a sandbox that never existed and fails as
  "session expired". `resume_run_id` is the sandbox, and for a cold run the two
  are equal — so read that one field either way. No template for resume — pass
  `--task` with what remains and how to fix it.
- **Two obstacles, then take over**: if the worker hits the same obstacle
  twice and still does not pass → the main session takes over and wraps up.
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
