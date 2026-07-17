---
name: kimi
description: Plan with Codex, then run a bounded task through the fixed Kimi K3 OpenCode worker and have Codex verify the result. Invoke explicitly as `$kimi [code|explore|test|review|resume] [task]`; examples include `$kimi code`, `$kimi code 修复登录问题`, `$kimi review 当前改动`, and `$kimi resume ses_xxx 继续修复`. Use whenever the user asks to delegate coding, investigation, testing, review, or session continuation to Kimi.
---

# Kimi Worker

Treat `$kimi` as the short user-facing entry point. Keep the long
`dispatch-opencode-workers` skill internal.

Follow the shared flow in
`${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/references/entry-flow.md`
for parsing (`code`/`explore`/`test`/`review`/`resume`, otherwise default to `code`),
dispatch, monitoring, and verification. This entry's specifics:

| Item | Value |
| --- | --- |
| Provider argument | `--provider kimi-k3` |
| Route | `kimi-worker/k3` at the official Kimi Code endpoint |
| Credential setup | `python3 <backend>/scripts/configure_credentials.py kimi-k3` |
| Live log | `$CODEX_HOME/opencode-workers/logs/kimi-k3/latest.log` |

This entry selects Kimi for this dispatch only. Invoke another provider or an additional
worker review only when the user explicitly asks for it.

Examples:

- `$kimi code` -> Kimi `code`, current task.
- `$kimi 修复登录问题` -> Kimi `code`, task `修复登录问题`.
- `$kimi explore 查清支付回调链路` -> Kimi `explore`.
- `$kimi review` -> Kimi `review`, current changes.
