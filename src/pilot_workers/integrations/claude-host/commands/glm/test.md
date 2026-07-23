---
description: Dispatch GLM to run tests and gather failure information (read-only, no fixes)
argument-hint: [what tests to run]
---

The user wants GLM to run tests:

$ARGUMENTS

Do the following:

1. **Make clear what to run.** Write the test command, directory, and preconditions as a self-contained task file (GLM is a separate OpenCode process and cannot see this conversation). If nothing is specified, take a quick look at how the project runs its tests, fix the command, then dispatch. General discipline (run, do not fix; reporting format) **does not need to be written** -- dispatch injects `prompts/test.md` automatically; the task only states which command to run and any known pre-existing failures. The structure is provided by the `pilot-workers template test` template; just fill in the blanks.

2. **Dispatch (the protocol is fixed in the pilot-workers CLI; do not invent your own flow):**

   1. `pilot-workers template test > /tmp/glm-test-<task-slug>-<timestamp>.md`, filling the task requirements into the template (unique naming prevents parallel sessions from clobbering each other). The worker is a separate process and cannot see this conversation, so the content must be self-contained.
   2. Run in the background with Bash (`run_in_background: true`):
      ```bash
      pilot-workers dispatch --provider glm --mode test --workdir "$PWD" --task-file /tmp/glm-test-<task-slug>-<timestamp>.md
      ```
   3. stdout is exactly two lines of JSON: the first line is `worker_runner.started` (record the run_id and log path); the last line is `worker_runner.verdict`. **The completion signal = this background Bash exiting on its own**; do not poll by process name, do not read the shared log to make any judgment (latest.log is for humans only).
   4. Verdict handling: `completed` -> `final_text` is the full worker report; sanity-check it: has counts and raw error text -> trust it; just "all passed" with no counts -> suspicious, read `jsonl_path` to see what command it actually ran, and redispatch if needed; run `git status` to confirm it left no junk changes; `step_capped_partial` -> partial coverage, report the uncovered scope truthfully; `empty`/`error` -> read `jsonl_path` first to do a post-mortem, then report the cause of death truthfully; never draw a conclusion without reading the evidence.

   Do not edit a worker's target files while it is running.

3. **Once you have the failure list, diagnosis and fixes are your job.** Use `file:line` to locate the cause and decide the fix; route large batches of mechanical fixes through `/glm:code`, and do small fixes yourself.

4. **Report honestly**: how many passed/failed, your read on the cause of failure, and how you plan to fix it.

If what to run is unclear, ask the user first; do not guess the command.
