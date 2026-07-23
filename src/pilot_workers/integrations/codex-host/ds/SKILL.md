---
name: ds
description: Plan with Codex, then run a bounded task through the fixed DeepSeek OpenCode worker and have Codex verify the result. Invoke explicitly as `$ds [code|explore|test|review|resume] [task]`; examples include `$ds code`, `$ds code fix the login bug`, `$ds review current changes`, and `$ds resume ses_xxx continue the fix`. Use whenever the user asks to delegate coding, investigation, testing, review, or session continuation to DeepSeek.
---

# DeepSeek Worker

Parse the first word after `$ds` as the mode (`code`/`explore`/`test`/`review`/`resume`); otherwise default to `code` and treat all text as the task. Keep planning, task decomposition, and final verification with Codex; give the worker only settled decisions.

1. `pilot-workers template <mode> > /tmp/ds-<mode>-<slug>-<timestamp>.md`, then fill in the template with the task (unique filename — parallel sessions must not collide). The worker is an independent process and cannot see this conversation; the file must be self-contained. No template for `resume` — pass `--task "<what remains, how to fix>"` instead.
2. Run in a background shell: `pilot-workers dispatch --provider ds --mode <mode> --workdir <absolute-project-path> --task-file <file>`. For `resume` add `--session <session_id>` and use the workdir from the prior `started` event.
3. stdout is exactly two JSON lines: first `worker_runner.started` (note run_id and log paths), last `worker_runner.verdict`. Completion signal = the background shell exiting. Never poll by process name; never judge from shared logs. Live log for humans: `$PILOT_WORKERS_HOME (default $CODEX_HOME) /opencode-workers/logs/ds/latest.log`.
4a. If the background shell exits but stdout has fewer than two lines, check its stderr — early failures (missing credentials, runner not installed) produce no JSON.
4. Act on `verdict`: `completed` → `final_text` is the worker's report; `step_capped_partial` → partial coverage, report honestly; `empty`/`error` → read `jsonl_path` for the post-mortem before concluding anything. Prefer `resume` over cold re-dispatch when a run fails or falls short; after the same obstacle twice, take over yourself.
5. After `code`/`resume`: inspect the actual diff against the task scope and run the smallest real verification yourself. Never treat the worker's completion claim as proof. Missing credentials → `pilot-workers credentials ds`.
