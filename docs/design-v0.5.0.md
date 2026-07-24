# Design v0.5.0 — direct-CLI architecture, structured verdicts, per-run sandboxes

Status: **FROZEN** (2026-07-24). Converged after 6 review rounds by three independent
model reviewers (Kimi, GLM, DeepSeek); final round verdict: 3 × unconditional go.
This document is the single normative reference for the v0.5.0 implementation.
Worker task files cite sections by `D<n>` / note number.

## Goal

Remove the Claude-subagent layer entirely. The planner (main session) drives the
CLI directly; the CLI returns mode-specific structured results; workers run in
fully isolated per-run sandboxes.

```
主会话 planner（一个 pilot-workers skill 教用法）
  → pilot-workers dispatch / fanout  （并行、看门狗、结构化 verdict）
    → runner 适配器（引擎事件 → UnifiedEvent）
      → OpenCode 子进程（per-run 沙箱）→ 模型
返回：stdout = mode 专属结构化 result + final_text_path；全文落盘按需读
```

Motivating incident (2026-07-24): a Claude subagent dispatched a worker in a
background shell, ended its turn while "waiting", and the completed worker's
verdict was never harvested. Subagents die at turn end; only the main session is
re-woken by background-task notifications. The fix is structural: no subagent hop.

---

## D1 — Integration layer: one playbook skill per host

Delete: 12 Claude agents (`integrations/claude-host/agents/`), 8 slash commands
(`integrations/claude-host/commands/`), 3 per-provider codex skills
(`integrations/codex-host/{glm,kimi,ds}/`).

Add: ONE host-agnostic `pilot-workers` skill per host (claude, codex). The skill
is a **playbook**, not CLI syntax docs. Structure: Quick Reference at top, then
per-mode sections. Normative doctrine list (all must appear):

1. Review axis-splitting + aggregation (2-4 orthogonal axes, one dispatch each).
2. Worth-it self-check — code: "spec longer than the diff → don't dispatch";
   test: "suite under 1 minute → run it yourself".
3. Cross-model review for rewrite-scale diffs (dispatch a *different* provider
   to review; two models' errors are uncorrelated).
4. Fanout recipe for parallel review axes (`pilot-workers fanout --job ...`).
5. `--worktree` required for parallel code-mode jobs; one worker per background
   Bash; batch similar-duration jobs per fanout, split mixed durations.
6. Resume-first recovery; after the same obstacle twice, take over yourself.
7. Spot-check discipline — review: verify 1-2 high findings at cited file:line;
   explore: verify 2-3 critical conclusions; unreferenced conclusions untrusted.
8. Test anti-false-positive: "all passed with no counts → suspicious → read
   jsonl_path to see what actually ran"; check `git status` for junk files.
9. Verdict-state handling matrix (all cells incl. completed+timed_out,
   step_capped+parsed; see D3).
10. Consumption contract: consume `verdict.result` first; Read `final_text_path`
    at most once, only when `parse_state != "parsed"` or a finding needs
    surrounding context; never auto-redispatch for parse reasons alone.
11. Dispatch from the MAIN session only (background Bash); never from a subagent.

Provider info: provider YAML gains optional `strengths`, `suitable_modes`,
`notes` fields, surfaced by `pilot-workers status`. The skill keeps a MINIMAL
static provider table stamped "as of v0.5.0 — verify with `pilot-workers status`
when uncertain".

## D2 — Install grammar and manifest v3

Grammar: `install <host|all>`, `uninstall <host|all>`,
`install|uninstall runner <name>`. The provider dimension and the `on` keyword
are gone; "skill" never becomes a grammar token. `status` becomes host-level for
installs; credential status stays per-provider.

Manifest v3: `{"schema_version": 3, "installs": {"<host>": {...}}}`.

Migration (one-time, on first v3 install): walk `installs[host]` purging the
legacy v1 `__all__` entry FIRST via `_purge_entry`, then every v2 provider entry
(the v2 entries record every old agent/command file path, so this removes all
legacy files). Purge prints one line per removed file. Content-hash warning is
dropped (unimplementable for v2 entries; silent overwrite is existing semantics).
After writing v3, `os.replace` the manifest file so no v1/v2-format file survives
to be re-migrated.

## D3 — Structured verdict (schema_version 2, breaking)

### Worker output contract

Prompts per mode instruct: write the normal report per the mode discipline, THEN
end with a literal fill-in-the-braces block; nothing after the block:

```
<!--PILOT_RESULT_BEGIN-->
{ ...mode-specific JSON... }
<!--PILOT_RESULT_END-->
```

Per-mode result schemas:

- explore: `{facts: [{fact, file_line}], truncated: bool, more_in: [dirs]}`
- code:    `{status, files_changed: [], validation: {commands: [{cmd, exit_code,
           output_summary}], passed: bool}, remaining_risks}`
- test:    `{command, passed: int, failed: int, failures: [{test, error}]}`
           (`error` capped at 40 lines with a truncation marker)
- review:  `{overall, severity_counts: {high, medium, low}, findings:
           [{severity, file_line, summary, impact, suggested_fix}]}`
- resume:  reuses the code schema.

### Extraction (deterministic, in dispatch.py — no LLM)

Find the LAST `PILOT_RESULT_BEGIN` marker, take content to the END marker,
tolerate trailing prose, validate against the mode schema.

Sibling discriminator field: `parse_state: "parsed" | "unstructured" |
"unavailable"`; `result` = mode schema or null. `state` is not used inside
result schemas.

### Classification (frozen order, full-matrix tested)

1. `steps >= cap` → `step_capped_partial` (any parse_state; a parsed partial
   block is still salvaged into `result`).
2. `parse_state == "parsed"` → `completed` (including crash-after-block;
   exit_code preserved in the verdict — intentional change from v0.4.0,
   documented in the consumer spec).
3. (a) summary-error AND `parse_state != "parsed"` → `error`;
   (b) summary ABSENT AND error event present AND
   `len(final_text) < EMPTY_FINAL_TEXT_THRESHOLD` → `error`
   (preserves v0.4.0 dispatch.py:238-240 parity: long unstructured text with a
   transient error event falls through to rule 4).
4. `parse_state != "parsed"` (unstructured OR unavailable) AND
   `len(final_text) >= threshold` → `completed` (v0.4.0 parity: any long text
   is a usable report; consumer reads final_text_path per the three-way rule).
5. else → `empty`.

`timed_out` / `idle_timed_out` / `interrupted` are sibling flags set
independently; consumers MUST check them even on `completed`.

### Artifacts

- `<run_id>.report.md`: ALWAYS written when dispatch writes a verdict —
  verbatim last text event when one exists, else the single sentinel line
  `no model output`. The authoritative empty signal is
  `parse_state == "unavailable"`, never report content. NO stripping of the
  result block (report.md stays consistent with the JSONL last text event).
- stdout verdict drops `final_text`, carries `final_text_path` → report.md.
- `--reparse` also writes report.md (idempotent, deterministic from JSONL).
- On-disk verdict.json and report.md writes are ATOMIC: NamedTemporaryFile in
  target dir + fsync + `os.replace` (pattern of install.py `_write_manifest`),
  **plus explicit chmod 0600 after replace**; JSON serialization stays compact
  single-line (existing stdout contract).
- Synthesized fanout verdicts: full v2 schema shape + `synthesized: true` +
  `reason` enum (`crash|timeout|idle_timeout|interrupted`) + `stderr_tail`;
  `result = null`, `final_text_path = null`. Consume rule: synthesized → read
  reason + stderr_tail only. Non-synthesized verdicts never carry `reason`;
  a dispatch-emitted `parse_state == "unavailable"` is consumed via sibling
  flags + `stderr_path`.

## D4 — Fanout hardening

- Children spawned with `start_new_session=True`.
- Explicit main-thread SIGINT handler AND a mirroring SIGTERM handler: set the
  shared interrupted flag, record `reason` into shared per-job state, then
  killpg every tracked child group (SIGTERM → TERMINATE_GRACE_SECONDS →
  SIGKILL). Handlers return (never sys.exit) — the existing flow prints exactly
  one JSON array. A second SIGINT restores the default handler (hard-kill
  escape). SIGKILL orphan window documented; bounded by child `--timeout`
  **unless it is 0** (see the C1 forfeiture note below).
- **Reason ordering**: reason is written to shared per-job state BEFORE killpg,
  always.
- **Single-writer rule**: `_run_job` is the only writer of `results[index]`.
  The watchdog and signal handlers never write verdicts. Exemption: the
  post-pool single-threaded backfill for never-started/cancelled futures runs
  on the main thread strictly after all `_run_job` threads have joined, filling
  only null slots.
- **Reason precedence**: a recorded reason synthesizes a verdict ONLY when the
  child emitted no verdict line on stdout. A captured real dispatch verdict is
  authoritative; its own sibling flags stand; the reason is logged to stderr.
- **Per-job watchdog**: activity = shared per-job `last_activity` timestamp,
  updated only by `_run_job` (stdout lines) and a dedicated stderr-reader
  thread (stderr lines); the watchdog polls read-only and never touches pipes.
  The dispatch child emits a prefix-tagged "harvesting" heartbeat line to
  stderr every few seconds during its epilogue (JSONL parse + verdict/report
  write) from a dedicated thread; heartbeat lines are filtered out of
  `stderr_tail`. Deadline = child `--timeout` + TERMINATE_GRACE_SECONDS +
  harvest allowance; silence ≥ TERMINATE_GRACE after the deadline → record
  reason, killpg. Absolute ceiling MAX_EPILOGUE_SECONDS after the deadline:
  kill regardless of activity.
- **Disabled timeout (`--timeout 0`)**: the parent watchdog is disabled for
  that job (no deadline, no silence kill, no MAX_EPILOGUE); the child's
  `--idle-timeout` remains the liveness bound. The skill warns that
  `--timeout 0 --idle-timeout 0` together make a job fully unbounded.
- **Spawn race**: after `procs[index] = proc`, `_run_job` re-checks the shared
  interrupted flag and killpgs immediately if set.
- Exit code: 0 requires every verdict in SUCCESS_VERDICTS AND
  `timed_out == idle_timed_out == interrupted == false` for all jobs.
- `cancel_futures=True` on shutdown; `--max-parallel` validated ≥ 1.

## D5 — Per-run sandbox

```
providers/<key>/
├── data/opencode/auth.json          ← 凭据正本（全局唯一）
├── cache/                           ← per-provider 共享（顺序派发保温）
└── runs/<run_id>/                   ← 每次 dispatch 供给
    ├── config/  data/  state/       ← per-run 隔离（XDG 指进来）
    ├── data/opencode/auth.json → 正本符号链接（零拷贝）
    ├── cache → ../../cache          ← 符号链接共享
    └── .lock                        ← {pid, started_at}，O_CREAT|O_EXCL
```

- pilot-workers manages ONLY its own data and the sandbox SHELL; it never
  inspects OpenCode's internals. Sandboxes are provisioned empty and reaped
  wholesale.
- auth.json zero-copy symlink; the 0600 permission check (stat follows the
  symlink to the canonical file) is pinned by a test. Fallback ONLY if OpenCode
  rejects symlinked auth: copy + unconditional delete in the child's `finally`;
  the reaper is the documented backstop for the SIGKILL case (residual window =
  sandbox retention; sandbox dirs 0700).
- **Probe results (2026-07-24, opencode 1.18.4 — Phase 0 COMPLETE)**:
  (1) Symlinked auth.json ACCEPTED — a real model call through a sandbox whose
  `data/opencode/auth.json` was a symlink to the canonical file succeeded
  (exit 0, no auth error); zero-copy symlink is the implementation, the
  copy-fallback path is NOT needed. (2) NO native data-dir/stateless flag
  exists (`opencode --help` / `run --help` audited) — XDG pointing is the
  mechanism. (3) Two concurrent runs sharing one `XDG_CACHE_HOME` both
  succeeded; cache pressure is inherently low because `runner_environment`
  already sets `OPENCODE_DISABLE_LSP_DOWNLOAD=1` and
  `OPENCODE_DISABLE_MODELS_FETCH=1` — shared per-provider cache is confirmed.
  Bonus finding: the session store is **WAL-mode SQLite at
  `data/opencode/opencode.db`** (with `-shm`/`-wal` siblings) — per-run
  isolation of `data/` is therefore mandatory, exactly as designed; `state/`
  holds OpenCode's own lock directories.
- **Lockfile**: JSON `{pid, started_at}` created with `O_CREAT|O_EXCL` (two
  racing acquisitions pinned by test). `started_at` is captured AT LOCK ACQUIRE
  from the SAME source the staleness checker reads — Linux:
  `/proc/<pid>/stat` field 22 (parsed after the last `)`); macOS:
  `LC_TIME=C ps -o lstart= -p <pid>` (locale-pinned). Never `datetime.now()`.
  Staleness = pid dead (`os.kill(pid, 0)` fails) OR live pid's start time does
  not match recorded `started_at`. If the platform source is unavailable, fall
  back to pid-dead-only. Both formats pinned by tests.
- **`maintain runs` subcommand**: atomically reaps logs + report + sandbox per
  run_id; `.report.md` added to the `_run_pairs` suffix set; keeps newest N per
  provider; retention knob independent of log cleanup; skips sandboxes with a
  live (non-stale) lock. The reaper lstat-walks and unlinks EVERY symlink it
  encounters (auth.json and cache are guaranteed-critical examples, not an
  allowlist) and never invokes bare `shutil.rmtree` on the sandbox root; a
  survival test covers an arbitrary worker-created symlink pointing outside
  the sandbox.
- **Resume**: keyed by `--session` + `--run-id` (both from the original
  verdict; explicit plumbing through dispatch/run). Locates the original
  sandbox via run_id; acquires the lock ONLY when absent or stale; a live lock
  → loud error "run is still active". If the sandbox was reaped → loud error
  "session expired past retention; redispatch cold" (resume window == sandbox
  retention window, documented in the skill).
- Known limitation (documented): a dispatch child in uninterruptible sleep
  (D state) survives killpg; SIGKILL is queued but takes effect only when the
  kernel I/O completes (or reboot). Its lock reads live; manual intervention
  required.

## D6 — Miscellaneous

- `opencode --version` check cached keyed on `(path, mtime_ns, size)`; cleared
  at the end of `install runner`; shared by `status`.
- Docs (README, CLAUDE.md, docs/architecture.md incl. stale test counts) and
  tests updated in the SAME commit as each behavior they encode.
- The D5 commit records before/after `cache_read` measurements on a repeated
  workload.

---

## Implementation deviations (recorded post-implementation, 2026-07-24)

- **`cancel_futures` replaced by spawn-race kill** (D4): signal handlers absorb
  SIGINT/SIGTERM without raising, so the executor shuts down normally and
  `cancel_futures` would never fire. Queued jobs instead spawn and are killed
  immediately by the post-`Popen` interrupted-flag re-check. Safety-equivalent
  (no new work proceeds after interrupt); costs one instant-killed process
  spawn per queued job.
- Watchdog poll interval is a fixed `WATCHDOG_POLL_INTERVAL = 0.05s` constant;
  `MAX_EPILOGUE_SECONDS` is read once at watchdog-thread start (not per
  iteration). Both are test-compatible and intentional simplifications.
- `_kill_job_group` uses `os.killpg(proc.pid, ...)` directly — valid because
  `start_new_session=True` guarantees pid == pgid.
- Provider metadata (`strengths`/`suitable_modes`) was corrected post-draft to
  match the project's established division of labor: ds → explore/test/review,
  kimi-k3 → code/review/explore, glm → code/review.
- The E2E pipeline self-verified on 2026-07-24: a production DS test-mode run
  returned `parse_state: "parsed"` with the test schema (`passed: 289`), a
  per-run sandbox with symlinked auth/cache, and a released lock.

## Review provenance

| Round | Scope | Result |
|---|---|---|
| 1 | remove-agents proposal, 3 axes (Kimi) | direction validated |
| 2 | design v2, 6 dimensions × 3 reviewers | go-with-changes ×3; 12 findings (R1-R12) |
| 3 | v3 = v2 + R-resolutions | R1-R12 closed ×2 reviewers; new lifecycle findings |
| 4 | v4 = v3 + A1-A10 | go-with-changes ×3; findings → B-series |
| 5 | v5 = v4 + B1-B12 | GLM go; Kimi/DS 3 mediums → C-series |
| 6 | v6 = v5 + C1-C7 | **go ×3 — converged** |

Adjudications of reviewer conflicts are recorded in the session that produced
this document; the deciding rationale for each is inlined above at the point of
the rule it affected (e.g. the rule-3 length guard, the JSON-block-vs-
section-regex extraction choice, cache sharing).
