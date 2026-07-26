# Mode: test — dispatch playbook

(Read this before every test dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

- The task states the exact test command, directory, preconditions, and known
  pre-existing failures. Run-and-gather only; fixes are your job.
- **Anti-false-positive**: counts + raw error text → trust it. "All passed"
  with no counts → suspicious → read `jsonl_path` to see what command
  actually ran; redispatch if needed. Run `git status` to confirm the worker
  left no junk changes behind.
- Bring counts and the failure list (test name, raw error text, `file:line`)
  back verbatim; root-cause analysis is your job.
