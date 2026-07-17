---
description: 派 Kimi 跑测试并收集失败信息（只读，不修复，进度实时转播进对话）
argument-hint: [跑什么测试]
---

用户想让 Kimi 跑测试：

$ARGUMENTS

请这样做：

1. **先自检划算度——快的套件直接劝退。** 先本地估一下：套件全量耗时 < 1 分钟、或失败输出可预期很短（几十行）→ 直接跟用户说「这个我自己跑更快」，然后自己跑。Kimi 的价值场景是：输出巨大（成百上千行失败要筛）、需要反复重跑收集、或你的上下文快满了。

2. **明确跑什么。** 测试命令、目录、前置条件写成自包含任务（Kimi 是独立 OpenCode 进程看不到本对话）。没指明就先看一眼项目用什么跑测试，确定命令再派。通用纪律（只跑不修、汇报格式）**不用写**——runner 自动注入 `prompts/test.md`；任务里只写跑什么命令、已知的预存失败。

3. **主会话直接后台派活（不走 subagent——这样进度才能实时进对话）。** 同一轮里做两件事：

   a. Bash（`run_in_background: true`）：
      ```bash
      python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
        --provider kimi-k3 --mode test \
        --workdir "$PWD" \
        --task-file /tmp/kimi-test-xxx.md
      ```

   b. 立刻挂低噪 Monitor（只报错误与完成——每条事件都会唤起主会话烧一轮对话，
      全量转播已被用户判为低效；中间过程用户自己 tail 日志可见）：
      ```
      Monitor:
        command: tail -n 0 -f ~/.codex/opencode-workers/logs/kimi-k3/latest.log | grep -E --line-buffered '== 完成|!! '
        description: Kimi 测试错误与完成信号
        timeout_ms: 1800000
        persistent: false
      ```
      常规事件通知不用回话；需要看中间过程时临时读日志文件，别放宽过滤器。
      看到连续多条 `!! `（权限拦截/报错）→ worker 卡住了：读日志确认，必要时 TaskStop 杀掉重派或自己跑，别干等。

4. **后台任务退出后**：TaskStop 停掉 Monitor，核对结果——有统计数字和报错原文就采信；只有一句「全部通过」没有统计 → 可疑，翻日志看它到底跑了什么；`git status` 确认没留下垃圾改动。

5. **拿到失败清单后，判断和修复是你的活。** 基于 `file:line` 定位原因、定修复方案；大批量机械修复走 `/kimi:code`，小修自己动手。

6. **诚实汇报**：几过几挂、失败原因的判断、打算怎么修。

要跑什么都不清楚就先问用户，别瞎猜命令。
