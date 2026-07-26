# Mode: resume — dispatch playbook

(Read this before every resume dispatch. The core SKILL.md holds the
priority chain, the red lines and the verdict contract; this file
holds the craft for this one mode.)

- **Resume-first recovery** — it reuses the full prior-session context and
  saves minute-scale round-trips versus a cold restart:

  ```bash
  pilot-workers dispatch --provider <key> --mode resume \
      --session <session_id from the verdict> --run-id <resume_run_id from the verdict> \
      --workdir <workdir from started> --task "Previous task incomplete: <what is missing, how to fix>"
  ```

  Take `--session` from the verdict and `--run-id` from its **`resume_run_id`**,
  not its `run_id`: a resumed run gets a fresh `run_id` for its own log, so
  resuming twice off `run_id` names a sandbox that never existed and fails as
  "session expired". For a cold run the two are equal. No template —
  pass `--task` with what remains and how to fix it.
- **Two obstacles, then take over**: same obstacle twice and still not
  passing → Codex takes over and wraps up.
- The resume window == the sandbox retention window. A loud "session expired
  past retention" error means the sandbox was reaped — redispatch cold. A
  loud "run is still active" error means a live lock; do not force it.
