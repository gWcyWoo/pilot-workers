---
description: 派 GLM 跑测试并收集失败信息（只读，不修复）
argument-hint: [跑什么测试]
---

用户想让 GLM 跑测试：

$ARGUMENTS

请这样做：

1. **明确跑什么。** 测试命令、目录、前置条件写成自包含 task file（GLM 是独立 OpenCode 进程看不到本对话）。没指明就先看一眼项目用什么跑测试，确定命令再派。通用纪律（只跑不修、汇报格式）**不用写**——runner 自动注入 `prompts/test.md`；任务里只写跑什么命令、已知的预存失败。写到 `/tmp/glm-test-xxx.md`。

2. **主会话直接后台派活（不走 subagent）。** 同一轮里做两件事：

   a. Bash（`run_in_background: true`）：
      ```bash
      python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
        --provider glm --mode test \
        --workdir "$PWD" \
        --task-file /tmp/glm-test-xxx.md
      ```

   b. 立刻挂低噪 Monitor：
      ```
      Monitor:
        command: tail -n 0 -f ~/.codex/opencode-workers/logs/glm/latest.log | grep -E --line-buffered '== 完成|!! '
        description: GLM test 完成信号
        timeout_ms: 1800000
        persistent: false
      ```

3. **派之前告诉用户**：GLM 干活期间主会话没有中间输出，实时进度看 `tail -f ~/.codex/opencode-workers/logs/glm/latest.log`。

4. **后台任务退出后**：TaskStop 停 Monitor，从 stdout 提取 `worker_runner.summary` 和最终文本。核对：
   - 结果里有统计数字和报错原文 → 采信
   - 只有一句「全部通过」没有统计 → 可疑，翻日志看它到底跑了什么命令，必要时重派
   - `git status` 确认没留下垃圾改动

5. **拿到失败清单后，判断和修复是你的活。** 基于带 `file:line` 的报错定位原因、定修复方案；方案定了之后如果是大批量机械修复，再走 `/glm:code`，小修自己动手。

6. **诚实汇报**：几过几挂、失败原因的判断、打算怎么修。

要跑什么都不清楚就先问用户，别瞎猜命令。
