---
description: 派 Kimi 探索代码库（只读），结论强制带 file:line
argument-hint: [要查什么]
---

用户想把这个探索任务派给 Kimi（读代码是 token 大头，这类活优先派出去）：

$ARGUMENTS

请这样做：

1. **直接把用户的问题写成 task file，不要自己先读代码。** 派 explore 就是为了省你的 token——你先 grep/read 一遍再派就白省了。你只做两件事：
   - 补上工作目录和范围（如果用户没说，就用 `$PWD`，范围写 `src/` 或整个项目）
   - 把下面这段输出纪律原文附进任务末尾

   > 输出纪律：
   > 1. 每条结论必须带 `file:line` 引用，没有引用的结论无效
   > 2. 结构化条目输出，一条一个事实；言简意赅，不写铺垫、总结、感想
   > 3. 禁止粘贴大段代码——需要引用时最多 3 行，多了给 `file:line` 让人自己看
   > 4. 结论总数不超过 20 条（任务方另有预算则从其规定）；超了说明问题问太宽，
   >    列最重要的并注明「还有 X 处未列出，在哪些目录」

   写到 `/tmp/kimi-explore-xxx.md`。

2. **主会话直接后台派活。** 同一轮里做两件事：

   a. Bash（`run_in_background: true`）：
      ```bash
      python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
        --provider kimi-k3 --mode explore \
        --workdir "$PWD" \
        --task-file /tmp/kimi-explore-xxx.md
      ```

   b. 立刻挂低噪 Monitor：
      ```
      Monitor:
        command: tail -n 0 -f ~/.codex/opencode-workers/logs/kimi-k3/latest.log | grep -E --line-buffered '== 完成|!! '
        description: Kimi explore 完成信号
        timeout_ms: 1800000
        persistent: false
      ```

3. **派之前告诉用户**：实时进度看 `tail -f ~/.codex/opencode-workers/logs/kimi-k3/latest.log`。

4. **后台任务退出后**：TaskStop 停 Monitor，从 stdout 提取 worker 的最终文本，**直接带回给用户**。不抽查、不复读、不改写——结论连同 `file:line` 原样转交。

5. 接下来要改码的话：规划是你的活；规划完机械落地的部分走 `/kimi:code`。

问题太模糊（连查什么都不清楚）就先问用户一句，别硬派。但「模糊」的标准是用户自己都不知道要查什么——不是你觉得问题不够结构化。用户说「查清 auth 怎么工作的」就够了，别要求他先列出具体文件。
