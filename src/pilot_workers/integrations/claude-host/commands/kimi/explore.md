---
description: Dispatch Kimi to explore the codebase (read-only); conclusions must carry file:line
argument-hint: [what to investigate]
---

The user wants to dispatch this exploration task to Kimi (reading code is the bulk of token spend, so this kind of work is a top candidate for dispatch):

$ARGUMENTS

Do the following:

1. **Write the user's question straight into a task file; do not read the code yourself first.** The whole point of dispatching explore is to save your tokens -- running grep/read yourself first wastes that saving. You only do two things:
   - Fill in the working directory and scope (if the user did not specify, use `$PWD` and scope to `src/` or the whole project)
   - Append the output discipline below verbatim to the end of the task

   > Output discipline:
   > 1. Every conclusion must carry a `file:line` reference; conclusions without a reference are invalid
   > 2. Output structured items, one fact per item; be terse; do not write preambles, summaries, or reflections
   > 3. Do not paste large code blocks -- when a quote is needed, cap it at 3 lines; for more, give `file:line` and let the reader look
   > 4. No more than 20 conclusions total (or follow the budget set by the task); going over means the question was too broad -- list the most important ones and note "X more not listed, in these directories"

   The structure is provided by the `pilot-workers template explore` template; just fill in the blanks.

2. **Dispatch (the protocol is fixed in the pilot-workers CLI; do not invent your own flow):**

   1. `pilot-workers template explore > /tmp/kimi-k3-explore-<task-slug>-<timestamp>.md`, filling the task requirements into the template (unique naming prevents parallel sessions from clobbering each other). The worker is a separate process and cannot see this conversation, so the content must be self-contained.
   2. Run in the background with Bash (`run_in_background: true`):
      ```bash
      pilot-workers dispatch --provider kimi-k3 --mode explore --workdir "$PWD" --task-file /tmp/kimi-k3-explore-<task-slug>-<timestamp>.md
      ```
   3. stdout is exactly two lines of JSON: the first line is `worker_runner.started` (record the run_id and log path); the last line is `worker_runner.verdict`. **The completion signal = this background Bash exiting on its own**; do not poll by process name, do not read the shared log to make any judgment (latest.log is for humans only).
   4. Verdict handling: `completed` -> `final_text` is the full worker report -- **bring it straight back to the user**, no spot-checking, no parroting, no rewriting; hand the conclusions on as-is with their `file:line`; `step_capped_partial` -> partial coverage, report the uncovered scope truthfully; `empty`/`error` -> read `jsonl_path` first to do a post-mortem, then report the cause of death truthfully; never draw a conclusion without reading the evidence.

   Do not edit a worker's target files while it is running.

3. If the next step is to change code: planning is your job; the mechanical-execution part of the plan goes through `/kimi:code`.

If the question is too vague (even the user does not know what to investigate), ask one clarifying question first; do not force a dispatch. But the bar for "vague" is that the user themselves does not know what to look into -- not that you think the question is not structured enough. "Figure out how auth works" is enough; do not require them to list specific files first.
