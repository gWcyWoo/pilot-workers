# Mode: code — dispatch playbook

(Read this before every code dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

- **Shape the task; the dispatch decision is already made.** Routing (or the
  user naming a provider) decided who does code work — what stays yours is
  making the task carryable: settle every architecture-level judgment call
  before dispatch (interfaces, boundaries, acceptance criteria — workers get
  only settled decisions) and hand over specs, not questions. A change that
  would need mid-course architecture judgment is a planning gap — settle
  more, then dispatch. Implementation-level choices inside those boundaries
  (what to reuse, internal structure) are the worker's; its prompt enforces
  reuse checks, so do not spell them out per task.
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
- A `reuse` field reporting cross-module duplication is an abstraction
  question landing on your desk: if the reported sites are not evidence
  enough, dispatch an abstraction-evidence explore; decide; hand any
  extraction to a NEW code task. Never bounce "clean this up" back to the
  worker — that returns an architecture decision to the weakest model.
- **Do not sample the diff.** A few hunks of a large change license nothing
  about the rest. Correctness comes from the test run, from exercising the
  behaviour end to end, and — for rewrite-scale diffs — from the cross-model
  review below. Never treat the worker's completion claim as proof.
  **The test run goes to whichever provider is assigned to `test` in the Workers
  table**, as a `test`-mode dispatch; running it here would void that
  assignment. You still read the counts and failures and decide what they mean.
- **Cross-model review for rewrite-scale diffs** (several hundred lines or more):
  dispatch a *different* provider in review mode before verifying — the two
  models' errors are uncorrelated, so the second catches the first's
  systematic blind spots at cheap quota. Skip for small changes.
- Do not edit a worker's target files while it is running. Deletion,
  migration, CI/keys/production config: do not dispatch; a human does those.
