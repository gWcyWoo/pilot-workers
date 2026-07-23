# OpenCode Worker Shared Foundation — Architecture

## 1. Who uses this foundation

```
Claude main session ──────┐
  /glm:code etc.           │       ┌──→ GLM  (glm-worker/glm-5.2)
  8 agents                 ├──→ run_worker.py ──→ OpenCode 1.18.3
  8 commands               │       └──→ Kimi (kimi-worker/k3)
Codex main controller ─────┘
  $glm / $kimi skills
```

**Both hosts share the same runner.** Any change takes effect on both sides at once — that is both the benefit and the constraint.

## 2. Directory layout

```
~/.codex/skills/dispatch-opencode-workers/
├── ARCHITECTURE.md          ← this document (shared contract)
├── SKILL.md                 ← Codex-side dispatch instructions
├── agents/openai.yaml       ← Codex entry metadata
├── prompts/                 ← worker system prompt (single source)
│   ├── common.md            ← shared across all modes
│   ├── code.md
│   ├── explore.md
│   ├── test.md
│   └── review.md
├── references/
│   ├── entry-flow.md        ← shared parse/dispatch/acceptance flow for $glm/$kimi
│   ├── provider-contract.md ← routing, isolation, security constraints
│   └── task-spec.md         ← task contract template
└── scripts/
    ├── providers.py         ← ★ single source of truth (Provider registry + version)
    ├── policy.py            ← permission matrix + config generation + prompt loading
    ├── runtime.py           ← environment/credentials/process/timeout/worktree
    ├── run_worker.py        ← thin CLI orchestration
    ├── fmt_events.py        ← live log rendering (convenience layer; failures must not affect the worker)
    ├── configure_credentials.py
    ├── install_runtime.sh   ← reads version from providers.py to install
    ├── maintain.py          ← log cleanup + worktree lifecycle
    └── tests/
        └── test_run_worker.py  ← 20 automated tests
```

Codex-side entry points:
```
~/.codex/skills/glm/SKILL.md      → --provider glm
~/.codex/skills/kimi/SKILL.md     → --provider kimi-k3
```

Claude-side entry points (not in this directory; listed for reference):
```
~/.claude/agents/{glm,kimi}-{coder,explorer,reviewer,tester}.md   ← 8 agents
~/.claude/commands/{glm,kimi}/{code,explore,review,test}.md       ← 8 slash commands
```

Runtime data:
```
~/.codex/opencode-workers/
├── providers/
│   ├── glm/        ← GLM isolated XDG (config/data/state/cache/auth)
│   └── kimi-k3/    ← Kimi isolated XDG
├── logs/
│   ├── glm/        ← GLM logs (JSONL + rendered latest.log + archives)
│   └── kimi-k3/
└── worktrees/      ← detached worktrees
```

## 3. Shared contract — what you cannot change freely

Both hosts depend on the following rules — **changing any one requires all verification conditions to pass at once**.

### 3.1 Provider routing (locked)

| User entry | provider arg | OpenCode Provider ID | Model ID | Endpoint |
|---|---|---|---|---|
| `$glm` / `/glm:*` | `glm` | `glm-worker` | `glm-5.2` | `https://open.bigmodel.cn/api/coding/paas/v4` |
| `$kimi` / `/kimi:*` | `kimi-k3` | `kimi-worker` | `k3` | `https://api.kimi.com/coding/v1` |

**Single source of truth**: the `PROVIDERS` dict in `scripts/providers.py`.
**Not allowed**: tasks overriding provider/model/endpoint/adapter; adding arbitrary models or relays; silently falling back to another provider.

### 3.2 OpenCode version (locked)

**Single source of truth**: `PINNED_OPENCODE_VERSION` in `scripts/providers.py`.
Current value: `1.18.3`.

- `install_runtime.sh` reads the version from `providers.py` (`python3 providers.py`); it is not hardcoded.
- `run_worker.py` verifies the binary version before every run; any mismatch fails immediately.
- Upgrade = contract change: change `providers.py` in one place → reinstall the runtime → run the full test suite → smoke-test each host once.

### 3.3 Credential isolation (cannot be relaxed)

- Each provider has its own XDG directory; auth.json permissions `0600`, directory `0700`.
- Keys never appear in CLI args, environment variables, task contracts, or ordinary logs.
- Atomic writes (tempfile + fsync + rename).
- If a key ever appears in stdout/stderr, it is replaced with `[REDACTED]`.
- Keys are sent only over HTTPS to the selected official provider, never through a relay.
- Never reuse Claude's keys, XDG, session, config, or skills.

### 3.4 Permission matrix (semantic constraint)

**Permission resolution in OpenCode 1.18.3 is last-match-wins** (`findLast`; insertion order is the priority).
This means the **insertion order of rules in `policy.py` is a correctness property**.

Key invariants:
- For the three read-only modes (explore/review/test), the `*>*` deny **must be inserted after all allows**; otherwise `rg x > f` would match the later `rg *` (allow) and leak.
- For code mode, the deny rules (`git push*`, `curl *`, `*auth.json*`, etc.) **must be inserted after `*: allow`**.
- After changing rule order, you must run `test_last_match_wins_resolution_matches_binary_semantics`.

**Rules you do not need to add (dead rules)**: `*|*`, `*&&*`, `*;*`, `*||*`, backticks, `*$(*`, `*<*`. OpenCode uses tree-sitter to split and match segments; each command node is checked against rules individually, and operators never appear in the node text.

### 3.5 Process isolation (cannot be relaxed)

- `--pure` + `--thinking` + `--format json`.
- The task contract is delivered via stdin, never argv.
- Sharing disabled, autoupdate disabled, all Claude Code compatibility loading off, all plugins off.
- The worker cannot delegate to sub-agents, cannot use MCP, cannot webfetch/websearch.
- Read-only modes forbid edit/write.
- Environment variables inherit via a whitelist (`SAFE_ENV_KEYS`); no `*API_KEY*` is inherited.

### 3.6 Output contract (format cannot change)

- First stdout line: `worker_runner.started` (JSON, contains run_id, log paths).
- Last stdout line: `worker_runner.summary` (JSON, contains session_id, exit_code, timed_out/idle_timed_out/interrupted).
- Both hosts parse these two events; a format change breaks both sides at once.

### 3.7 Log contract

- Raw JSONL (`<run_id>.jsonl` + `.stderr.log`): the authoritative record; cleaned up explicitly by `maintain.py`.
- Rendered `latest.log`: convenience layer, for `tail -f`. **A rendering failure must not affect worker execution or exit code.**
- Monitor grep markers: `== done` and `!! ` — the Claude-side Monitor greps these two strings directly.
- Archives keep 20 entries per provider.

### 3.8 Worker prompt (single shared source)

`prompts/*.md` is the worker's system prompt. The runner reads it in `policy.build_config` and injects it into the agent definition.

**The dispatch discipline for both hosts (value-for-money self-check, spec discipline, spot-checks, cross-review) does not live here** — those live in each host's respective agent/skill/command definitions. prompts/ holds only the worker's internal execution discipline.

## 4. What you may change, with verification

| What you change | Constraint |
|---|---|
| `prompts/*.md` content | Keep the report format `STATUS/FILES_CHANGED/VALIDATION/REMAINING_RISKS` unchanged |
| Add an allow rule in `policy.py` | Place it in the correct position (before the deny); run the permission tests |
| Rendering improvements in `fmt_events.py` | Keep the `== done` and `!! ` markers unchanged (Monitor depends on them) |
| Add features in `maintain.py` | Preserve "never silently delete the latest log" and "refuse to delete a dirty worktree" |
| `runtime.py` timeout defaults | Current values: timeout=3600s, idle=900s, heartbeat=60s |
| `references/*.md` docs | Keep them consistent with `providers.py` (drift-prevention tests assert this) |

## 5. Verification checklist — must all pass after any change

```bash
# 1. All 20 unit tests
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker

# 2. Dual dry-run (fields match the locked routing)
python3 run_worker.py --provider glm --mode code --workdir /tmp --task x --dry-run
python3 run_worker.py --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 3. Single-source version check
python3 providers.py  # should print 1.18.3 (current)

# 4. Credential status
python3 configure_credentials.py all --status
```

After major changes (provider routing, version upgrades, permission matrix, output format), additionally:
```bash
# 5. Real smoke test (consumes a small amount of quota)
python3 run_worker.py --provider glm --mode explore --workdir <project> --task "Read any file and report its first line."
python3 run_worker.py --provider kimi-k3 --mode explore --workdir <project> --task "Read any file and report its first line."
```

## 6. Files on the Codex side that need to be kept in sync

When the shared foundation undergoes any of the following changes, the Codex-side entry points must be synced:

| Foundation change | Codex files that must be synced |
|---|---|
| Add/remove a mode (e.g. adding `deploy`) | `glm/SKILL.md`, `kimi/SKILL.md`, `references/entry-flow.md` |
| Change CLI arguments | `glm/SKILL.md`, `kimi/SKILL.md`, `references/entry-flow.md` |
| Change log paths | `SKILL.md`, `references/entry-flow.md` |
| Change report format | `prompts/*.md`, `references/task-spec.md` |

**The 8 agents + 8 commands on the Claude side are not maintained by Codex**, but if a shared interface changes (CLI arguments, summary format, log paths), the user must be told to sync the Claude side as well.

## 7. Install / upgrade procedures

### First-time install

```bash
# 1. Install the pinned OpenCode runtime
bash ~/.codex/skills/dispatch-opencode-workers/scripts/install_runtime.sh

# 2. Configure credentials (interactive; key is not displayed)
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py glm
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py kimi-k3

# 3. Verify
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py all --status
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/run_worker.py \
  --provider glm --mode code --workdir /tmp --task x --dry-run
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/run_worker.py \
  --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 4. Run tests
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker
```

### Upgrading the OpenCode version

```bash
# 1. Update the single source of truth
# Edit ~/.codex/skills/dispatch-opencode-workers/scripts/providers.py
# Change PINNED_OPENCODE_VERSION = "new version"

# 2. Reinstall the runtime
bash ~/.codex/skills/dispatch-opencode-workers/scripts/install_runtime.sh

# 3. Run the full test suite
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker

# 4. Dual dry-run
python3 run_worker.py --provider glm --mode code --workdir /tmp --task x --dry-run
python3 run_worker.py --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 5. Real smoke test (one small task per provider)
python3 run_worker.py --provider glm --mode explore --workdir <project path> \
  --task "Read any file and report its first line."

# 6. Tell the user to sync the Claude-side smoke test (the user runs /glm:explore in Claude Code to verify)
```

### Log cleanup

```bash
# Delete logs older than 14 days (never delete each provider's most recent run log)
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  logs --older-than-days 14

# Clean up GLM only
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  logs --older-than-days 14 --provider glm
```

### Worktree management

```bash
# List all worker worktrees
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py worktrees list

# Remove a clean, integrated worktree (dirty or with un-integrated commits will be refused)
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  worktrees remove /absolute/path/to/worktree
```

## 8. Security boundary statement

The current permission model (OpenCode tool permissions + shell deny rules + credential isolation + XDG separation) **protects against mistakes, not malice**. It guards against the model slipping into a `git push`, opportunistically exfiltrating via `curl`, or reading the wrong credential file.

It does not protect against:
- Prompt injection from a malicious repo (`python3 -c "urllib.request.urlopen(...)"` contains no `curl` substring and bypasses all shell rules)
- The worker itself having OS-user-level permissions to read files

The malicious-repo scenario requires an OS/container sandbox — it cannot be solved by this layer of string rules. Do not market the current isolation as a security sandbox.
