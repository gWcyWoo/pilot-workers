---
description: Dispatch a coding task Claude has already planned to GLM for execution (modifies files directly; dispatch in the background, then harvest by verdict when done)
argument-hint: [task description]
---

The user wants to dispatch this coding task to GLM for execution:

$ARGUMENTS

Do the following:

1. **Planning is your job, not GLM's.** If the approach is not decided, think it through first; if you need to scope the code first, run `/glm:explore` -- do not read through it yourself.

2. **Before dispatching, self-check whether it is worth it: is the expected change volume far larger than the task description?**
   - Worth it: large batches of mechanical changes (renaming dozens of files, scaffolding boilerplate, backfilling tests in bulk), parallel fan-out, your quota near the cap
   - Not worth it: small tweaks, tasks that need mid-course judgment, self-contained specs about as long as the diff
   - **One worker call should bundle at most 2-3 related fix points; split larger batches into multiple workers (which can run in parallel).**

   If it is not worth it, just tell the user "this is better done by me" and do it yourself. Do not force a dispatch.

3. **Write the task as a self-contained spec.** GLM is a separate OpenCode process and cannot see this conversation, so the spec must be self-contained: explicit file paths, completion criteria, and the do-not-touch boundaries. **Pick sub-second verification commands** (grep/diff/typecheck); leave the heavyweight `pnpm test` for the main session to run. General discipline **does not need to be written** -- dispatch injects `prompts/*.md` automatically. Copy any out-of-project material into the project root first, then reference it. The structure is provided by the `pilot-workers template code` template; just fill in the blanks.

4. **Dispatch (the protocol is fixed in the pilot-workers CLI; do not invent your own flow):**

   1. `pilot-workers template code > /tmp/glm-code-<task-slug>-<timestamp>.md`, filling the task requirements into the template (unique naming prevents parallel sessions from clobbering each other). The worker is a separate process and cannot see this conversation, so the content must be self-contained.
   2. Run in the background with Bash (`run_in_background: true`):
      ```bash
      pilot-workers dispatch --provider glm --mode code --workdir "$PWD" --task-file /tmp/glm-code-<task-slug>-<timestamp>.md
      ```
   3. stdout is exactly two lines of JSON: the first line is `worker_runner.started` (record the run_id and log path); the last line is `worker_runner.verdict`. **The completion signal = this background Bash exiting on its own**; do not poll by process name, do not read the shared log to make any judgment (latest.log is for humans only).
   4. Verdict handling: `completed` -> `final_text` is the full worker report; `step_capped_partial` -> partial coverage, report the uncovered scope truthfully; `empty`/`error` -> read `jsonl_path` first to do a post-mortem, then report the cause of death truthfully; never draw a conclusion without reading the evidence.
   5. If the worker fails or does not converge, prefer resume (it reuses the full prior-session context and saves minute-scale round-trips versus a cold restart):
      ```bash
      pilot-workers dispatch --provider glm --mode resume --session <session_id from the verdict> --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
      ```
      If it hits the same obstacle twice and still does not pass -> the main session takes over and wraps up.

   For parallel workers, add `--worktree` so each gets an isolated git worktree that will not stomp the others; clean up with `pilot-workers maintain worktrees remove <path>` when done. When you want parallel fan-out without consuming main-session turns, route through the corresponding coder subagent instead (one instance manages one worker). Do not edit a worker's target files while it is running.

5. **Verification is the single verification pass -- do not skip it** (only when verdict == `completed` or `step_capped_partial`):
   - Run `git diff --stat` against the spec whitelist to catch out-of-bounds changes (exclude dirty changes that pre-existed before the session started)
   - Spot-check the actual diff with `git diff` to confirm it matches the plan
   - Run tests/lint
   - **For rewrite-scale diffs (hundreds of lines or more), dispatch `/kimi:review` for a cross-model review before verifying** (axes: correctness + spec conformance) -- the two models' errors are uncorrelated, so Kimi catches GLM's systematic blind spots, and you spend cheap quota. Skip this step for small changes.

6. **Report honestly**: what changed, what verification found, and what the user should look at themselves.

If the task description is too vague to dispatch, ask the user first; do not force a dispatch.
