# Mode: test-case — dispatch playbook

(Read this before every test-case dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

- **Shape the task with precision; the dispatch decision is already made.**
  Before writing the task file you MUST settle three things completely:
  1. **Objective** — what test coverage to achieve, stated as verifiable
     outcomes (e.g. "cover all branches of `parse_config()`", "add edge-case
     tests for empty input, oversize input, and malformed JSON").
  2. **Interface and steps** — the target code's entry points (file:line),
     the test framework and conventions to follow, any fixtures or mocks to
     use, and the directory where test files go. The worker must not guess
     any of these.
  3. **Boundaries** — what the worker must NOT do: never modify source code
     under test, never change existing tests unless the task says so, never
     add dependencies, never restructure the test directory. Spell out every
     constraint — an unlisted boundary is an invisible one.
- One dispatch covers test cases for one cohesive unit (a module, a class,
  a feature surface); split larger scopes into multiple workers.
- **Always dispatch with `--worktree`** — test-case writes new files and
  must not collide with a code worker running on the main workdir. See
  "Parallel test-case + code workflow" in the core SKILL.md for the full
  dispatch → merge → test → cleanup sequence.
- **After both workers finish:** commit the worktree's changes, merge into
  main (`git merge` or cherry-pick), then dispatch test on the merged tree.
  Clean up with `pilot-workers maintain worktrees remove <path>` once test
  passes.
- Verify by running the generated tests yourself. Compare the worker's
  `FILES_CHANGED` against `git diff --stat` + `git status --short` — any
  source file (non-test) in the diff is a boundary violation.
- **Do not merge test files blindly.** Read the generated test names and
  assertions; confirm they test the scenarios you specified, not just
  the happy path.
