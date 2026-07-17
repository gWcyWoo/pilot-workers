---
name: glm
description: Plan with Codex, then run a bounded task through the fixed GLM 5.2 OpenCode worker and have Codex verify the result. Invoke explicitly as `$glm [code|explore|test|review|resume] [task]`; examples include `$glm code`, `$glm code 修复登录问题`, `$glm review 当前改动`, and `$glm resume ses_xxx 继续修复`. Use whenever the user asks to delegate coding, investigation, testing, review, or session continuation to GLM.
---

# GLM Worker

Treat `$glm` as the short user-facing entry point. Keep the long
`dispatch-opencode-workers` skill internal.

Follow the shared flow in
`${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/references/entry-flow.md`
for parsing (`code`/`explore`/`test`/`review`/`resume`, otherwise default to `code`),
dispatch, monitoring, and verification. This entry's specifics:

| Item | Value |
| --- | --- |
| Provider argument | `--provider glm` |
| Route | `glm-worker/glm-5.2` at the official Zhipu Coding endpoint |
| Credential setup | `python3 <backend>/scripts/configure_credentials.py glm` |
| Live log | `$CODEX_HOME/opencode-workers/logs/glm/latest.log` |

This entry selects GLM for this dispatch only. Invoke another provider or an additional
worker review only when the user explicitly asks for it.

Examples:

- `$glm code` -> GLM `code`, current task.
- `$glm 修复登录问题` -> GLM `code`, task `修复登录问题`.
- `$glm explore 查清支付回调链路` -> GLM `explore`.
- `$glm review` -> GLM `review`, current changes.
