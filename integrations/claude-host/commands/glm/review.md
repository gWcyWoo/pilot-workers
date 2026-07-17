---
description: 派 GLM 审查代码（只读，不修复），多方向并行扇出
argument-hint: [审什么]
---

用户想让 GLM 审查代码：

$ARGUMENTS

请这样做：

1. **先定审查方向——这是判断活，是你的。** 根据被审对象拆出 2-4 个正交的方向，例如：正确性与边界条件、安全（注入/越权/密钥泄漏）、性能（热路径/N+1/泄漏）、一致性（与代码库既有约定的偏离）。每个方向写成一条自包含 task file：范围、这个方向具体看什么。GLM 是独立 OpenCode 进程看不到本对话。附上输出纪律：

   > 输出纪律：
   > 1. 每条发现的格式：`[高|中|低] file:line 问题一句话 —— 为什么是问题一句话`
   > 2. 没有 `file:line` 的发现无效；不确定的标注「存疑」而不是略过
   > 3. 禁止粘贴大段代码——需要引用时最多 3 行
   > 4. 不给修复方案，不写总结感想；发现总数不超过 15 条，超了列严重的并注明还有多少

2. **并行后台派活。** 同一轮里起多个后台 Bash（`run_in_background: true`），每个方向一个：
   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
     --provider glm --mode review \
     --workdir "$PWD" \
     --task-file /tmp/glm-review-方向名.md
   ```
   方向多时可以混用 kimi（`--provider kimi-k3`）分摊两边额度。

   同时挂一个 Monitor：
   ```
   Monitor:
     command: tail -n 0 -f ~/.codex/opencode-workers/logs/glm/latest.log | grep -E --line-buffered '== 完成|!! '
     description: GLM review 完成信号
     timeout_ms: 1800000
     persistent: false
   ```

3. **派之前告诉用户**：实时进度看 `tail -f ~/.codex/opencode-workers/logs/glm/latest.log`（并行时按行内 PID 标签区分各 worker）。

4. **全部 worker 退出后**：TaskStop 停 Monitor，从各 stdout 提取最终文本。

5. **汇总是你的活。** 各方向发现合并去重、按严重级别排序，**直接报给用户**。不抽查——发现带着 `file:line`，用户自己能验。修复方案你来定：小修自己动手，大批量机械修复走 `/glm:code`。

6. **诚实汇报**：每个方向几条发现、真问题是哪些（带 `file:line`）、打算怎么修。

审什么都不明确（连范围都没有）就先问用户，别对整个仓库无差别开火。
