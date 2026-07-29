---
name: pilot-workers
description: Executor layer — every time you are about to write code, run tests, explore code, or review a diff, check here FIRST. If a provider is assigned for that kind of work, it MUST execute it — not you. This does not change your workflow, task decomposition, or verification strategy; it only replaces WHO executes each step. Invoke explicitly as `$pilot-workers [code|explore|test|review|resume] [task]`. {{PILOT_PROVIDER_TRIGGERS}}
---

# pilot-workers Playbook

Parse the first word after `$pilot-workers` as the mode
(`code`/`explore`/`test`/`review`/`resume`); otherwise default to `code` and
treat all text as the task. Workers are separate OpenCode processes that
**cannot see this conversation**.

**pilot-workers is an executor layer, not a workflow layer.** Your workflow
(TDD or not, what order, how to verify, task decomposition) is unchanged.
The ONLY thing pilot-workers changes is WHO executes each step: before you
write code — check if code has an assigned provider; before you run tests —
check if test has one; before you explore or review — same check. If a
provider is assigned, dispatch to it. If not, do it yourself. This is the
entire contract. Workers get only settled decisions.

## Division of reasoning

Reasoning splits by level, and each level has exactly one owner:

- **You (host): architecture.** Business-flow reasoning — how the change
  decomposes, module boundaries, interfaces, the dependency order between
  tasks — plus integration-test planning WHEN the outer workflow has a test
  stage (e.g. TDD; no test stage outside, no test planning here). Whether
  to introduce a new shared interface or abstraction, and under which
  pattern, is your call too — decided on explore-gathered evidence and
  locked into the task, never left to a worker. And arbitration: consume
  verdicts, judge acceptance, integrate results.
- **code worker: implementation.** Inside the boundaries the task locks,
  HOW is the worker's: what existing code to reuse, local structure and
  helpers within its scope. Its prompt already enforces a
  search-before-edit gate and a post-implementation reuse check — do not
  micro-spec implementation in the task file, and do not redo its checks.
  Cross-module abstractions are not the worker's to invent: it reports the
  duplication evidence and you decide.
- **explore worker: evidence.** It reports facts with file:line — how a
  flow runs, what already exists, where duplication sits — to feed your
  reasoning; judgment stays with you. Not a file-by-file inventory, and
  never a recommendation.

**The dispatch test, above any mode:** delegate only work whose
verification is far cheaper than its execution — bulk reading whose
findings slot straight into planning, implementation against a contract
that tests check. Work with no cheap check (open-ended design, subtle
trade-offs) never goes to a worker: that would hand the system's quality
to its weakest model. Do it here.

Orchestration follows from the dependency graph your architecture reasoning
produces: tasks with no dependency between them go out concurrently via
`fanout`. explore/review jobs parallelize freely (read-only). Parallel code
jobs need isolation — disjoint file whitelists, or `--worktree` per job,
after which merging the worktrees back is your integration step.

**Explore orchestration — three standard lenses, one dispatch each, fanout
in parallel** (read-only jobs never collide):

1. **business flow** — how the flow under change runs today.
2. **architecture constraints** — for each capability this change needs
   (HTTP, storage, auth, ...), what already exists that the new code must
   fit into.
3. **abstraction evidence** — only when this change puts an abstraction
   question on the table: the duplication sites, how the copies differ,
   and the patterns this project already uses.

Every lens is scoped by THIS change's needs — never a repo-wide inventory
or duplication audit. Lens 2 is what puts "reuse `src/net/client.py`" into
the code task's Interfaces section; lens 3 is what lets YOU decide
abstract-or-not instead of a worker. Not every change needs all three —
a trivial fix may need none, a new feature may need 1 + 2.

**The explore worker is fast but weak — write the requirement in full.**
The task file must carry the complete context and turn every question into
a concrete lookup item; the worker must never have to infer what you meant.
Each sentence you save writing the task becomes a wrong guess made on a
weaker model.

## When this skill applies

Two kinds of moments route here; match either, then choose the provider by
the priority chain below.

| Moment | Mode |
| --- | --- |
| "how does X work" / "trace this flow" / "探索、梳理、理解、读一下这块代码" — or YOU need to read code to build understanding before planning | explore |
| "implement/fix/refactor X" / "改一下、实现、修复" — or the plan is settled and edits would begin | code |
| "run the tests" / "跑一下测试" — or you want a suite run to verify a change | test |
| "review this change" / "review 一下这次改动" — or a rewrite-scale diff just landed | review |
| a prior dispatch failed or stopped short | resume |

The self-initiated rows draw the line by PURPOSE, never by size: reading to
LEARN (trace a flow, understand a module, gather context for a change) is an
explore dispatch no matter how few files; verifying a KNOWN location (a cited
file:line, a returned diff, a verdict) is local reading and needs no worker.

## Quick Reference

0. **Choose `<key>` by priority.** (1) Explicit user input always wins: a
   provider the user names is used even where the Workers table assigns
   another, and an explicit request to do the work here keeps it here.
   (2) Otherwise the **Workers table** at the bottom of this file decides: a
   routed mode is ALWAYS dispatched to its assigned provider — never done in
   this session, and never weighed for whether it is worth a worker.
   `install ... for <mode>` was the user making that economics decision once;
   it is not yours to remake per task. If a routed dispatch seems wasteful,
   dispatch anyway and say so afterwards — the user can
   `pilot-workers uninstall for <mode> on <host>`. (3) **A mode with no
   provider assigned is not an invitation to guess: do it yourself.** No
   assignment means the user never delegated that kind of work; picking a
   worker by its advertised strengths would delegate something they chose to
   keep. If a mode looks worth delegating, say so and let them run
   `pilot-workers install <provider> on <host> for <mode>`. Never dispatch to
   a provider absent from the table: it was not configured for this host.
1. Read `modes/<mode>.md` (next to this file in the skill directory) — that
   mode's task recipe and verification contract. Never dispatch a mode whose
   playbook you have not read in this session.
2. `pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md`,
   then fill in the template (unique filename — parallel sessions must not
   collide). **Every task file must settle three things before dispatch:**
   (a) **Objective** — what the worker must deliver, stated as verifiable
   outcomes. (b) **Interfaces and entry points** — the entry points
   (file:line), interfaces, and conventions the implementation must fit;
   architecture is settled here, while implementation-level choices inside
   those boundaries (what to reuse, internal structure) belong to the
   worker. (c) **Boundaries** — what the worker must NOT do: files it
   must not touch, patterns it must not use, scope it must not exceed; an
   unlisted boundary is an invisible one.
   The task file must be **self-contained** and carry **never any
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
3. Run in a background shell **from the main session**:
   `pilot-workers dispatch --provider <key> --mode <mode> --workdir <absolute-project-path> --task-file <file>`.
   Subagents die at turn end and their background tasks are never harvested —
   never dispatch from a subagent, never end your turn to "wait".
4. stdout is exactly two JSON lines: first `worker_runner.started` (note
   run_id and log paths), last `worker_runner.verdict`. Completion signal =
   the background shell exiting on its own. Never poll by process name; never
   judge from shared logs (`latest.log` is for humans only). If the shell
   exits with fewer than two lines, check its stderr — early failures
   (missing credentials, runner not installed) produce no JSON.
5. Consumption contract: read `verdict.result` first (the parsed
   PILOT_RESULT block). Read `final_text_path` at most once, only when
   `parse_state != "parsed"` or a finding requires surrounding context. Never
   auto-redispatch on parse failure alone.
6. Resume-first recovery: on failure or non-convergence, resume with
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
