---
description: Dispatch GLM to review code (read-only, no fixes); fan out across multiple axes in parallel
argument-hint: [what to review]
---

The user wants GLM to review code:

$ARGUMENTS

Do the following:

1. **First fix the review axes -- this is judgment work and it is yours.** Split out 2-4 orthogonal axes from the target under review, for example: correctness and boundary conditions, security (injection/privilege escalation/secret leakage), performance (hot paths/N+1/leaks), consistency (deviations from existing codebase conventions). Write each axis as its own self-contained task file: scope, what specifically to look at under this axis. GLM is a separate OpenCode process and cannot see this conversation. Append the output discipline:

   > Output discipline:
   > 1. Format for each finding: `[high|medium|low] file:line one-sentence problem -- one-sentence why-it-matters`
   > 2. Findings without a `file:line` are invalid; mark uncertain ones "doubtful" rather than skipping them
   > 3. Do not paste large code blocks -- when a quote is needed, cap it at 3 lines
   > 4. No fix proposals, no summary or reflections; no more than 15 findings total -- if you go over, list the severe ones and note how many remain

   The structure is provided by the `pilot-workers template review` template; just fill in the blanks. When there are many axes, you can mix in kimi (`--provider kimi-k3`) to spread the load across both quotas.

2. **Dispatch (the protocol is fixed in the pilot-workers CLI; do not invent your own flow). Each axis is one independent task with one independent background Bash:**

   1. For each axis: `pilot-workers template review > /tmp/glm-review-<axis-name>-<timestamp>.md`, filling the task requirements into the template (unique naming prevents parallel sessions from clobbering each other). The worker is a separate process and cannot see this conversation, so the content must be self-contained.
   2. Launch multiple background Bash calls (`run_in_background: true`) in the same round, one per axis:
      ```bash
      pilot-workers dispatch --provider glm --mode review --workdir "$PWD" --task-file /tmp/glm-review-<axis-name>-<timestamp>.md
      ```
   3. Each stream's stdout is exactly two lines of JSON: the first line is `worker_runner.started` (record the run_id and log path); the last line is `worker_runner.verdict`. **The completion signal = that background Bash exiting on its own**; do not poll by process name, do not read the shared log to make any judgment (latest.log is for humans only). Each axis collects its verdict independently, without interference.
   4. Verdict handling: `completed` -> `final_text` is the full worker report; `step_capped_partial` -> partial coverage, report the uncovered scope truthfully; `empty`/`error` -> read `jsonl_path` first to do a post-mortem, then report the cause of death truthfully; never draw a conclusion without reading the evidence.

   Do not edit a worker's target files while it is running.

3. **Aggregation is your job.** Merge and dedupe findings across axes, sort by severity, and **report to the user directly**. Do not spot-check -- findings carry `file:line`, so the user can verify themselves. You decide the fix plan: do small fixes yourself, and route large batches of mechanical fixes through `/glm:code`.

4. **Report honestly**: how many findings per axis, which ones are real (with `file:line`), and how you plan to fix them.

If the scope of review is unclear (no range at all), ask the user first; do not fire indiscriminately across the whole repo.
