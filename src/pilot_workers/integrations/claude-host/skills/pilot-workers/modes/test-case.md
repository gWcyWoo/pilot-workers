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
- **ALL RED — zero exceptions.** The worker runs the generated tests and
  every single one must FAIL. A test that passes is not testing new
  behavior — the worker rewrites it until it fails. This is the contract.
- **This mode is batch test generation, not vertical-slice TDD.** One
  dispatch covers all test cases for a cohesive unit (a module, a class, a
  feature surface). Do NOT dispatch test-case once per test — that is
  vertical-slice TDD, and it stays in the main session: write one test
  yourself, dispatch code to make it green, repeat. Use test-case mode only
  when the interface is settled and you know the full test surface upfront.
  Split larger scopes into multiple workers, not into one-test dispatches.
- **Always dispatch with `--worktree`** — test-case writes new files and
  must not collide with a code worker running on the main workdir. See
  "Parallel test-case + code workflow" in the core SKILL.md for the full
  parallel sequence and merge timing.
- Verify by running the generated tests yourself. Compare the worker's
  `FILES_CHANGED` against `git diff --stat` + `git status --short` — any
  source file (non-test) in the diff is a boundary violation.
- **Do not merge test files blindly.** Read the generated test names and
  assertions; confirm they test the scenarios you specified, not just
  the happy path.
