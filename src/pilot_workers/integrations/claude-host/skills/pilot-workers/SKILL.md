---
name: pilot-workers
description: Executor layer — every time you are about to write code, run tests, explore code, or review a diff, check here FIRST. If a provider is assigned for that kind of work, it MUST execute it — not you. This does not change your workflow, task decomposition, or verification strategy; it only replaces WHO executes each step. {{PILOT_PROVIDER_TRIGGERS}}
---

# pilot-workers Playbook

You are the planner. Workers are separate OpenCode processes that **cannot see
this conversation**.

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

```bash
pilot-workers template <mode> > /tmp/<provider>-<mode>-<slug>-<timestamp>.md   # fill it in — self-contained
pilot-workers dispatch --provider <key> --mode <mode> --workdir "$PWD" --task-file <file>   # background Bash
```

**Choose `<key>` by priority.** (1) Explicit user input always wins: a provider
the user names is used even where the Workers table assigns another, and an
explicit request to do the work here ("don't dispatch", "do it yourself") keeps
it in this session. (2) Otherwise the **Workers table** at the bottom of this
file decides: a routed mode is ALWAYS dispatched to its assigned provider —
never done in this session, and never weighed for whether it is worth a worker.
`install ... for <mode>` was the user making that economics decision once; it
is not yours to remake per task. If a routed dispatch seems wasteful, dispatch
anyway and say so afterwards — the user can
`pilot-workers uninstall for <mode> on <host>`. (3) **A mode with no provider
assigned is not an invitation to guess: do it yourself, in this session.** No
assignment means the user never delegated that kind of work; picking a worker
by its advertised strengths would delegate something they chose to keep. If a
mode looks worth delegating, say so and let them run
`pilot-workers install <provider> on <host> for <mode>`. Never dispatch to a
provider absent from the table: it was not configured here.

**Then Read `modes/<mode>.md`** (next to this file in the skill directory)
before writing the task file — it is that mode's task recipe and verification
contract. Never dispatch a mode whose playbook you have not read in this
session.

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

- **Every task file must settle three things before dispatch:**
  1. **Objective** — what the worker must deliver, stated as verifiable outcomes.
  2. **Interfaces and entry points** — the entry points (file:line),
     interfaces, and conventions the implementation must fit. Architecture
     is settled here; implementation-level choices inside those boundaries
     (what to reuse, internal structure) belong to the worker.
  3. **Boundaries** — what the worker must NOT do: files it must not touch,
     patterns it must not use, scope it must not exceed. An unlisted boundary
     is an invisible one.
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
