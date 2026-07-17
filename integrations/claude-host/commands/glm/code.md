---
description: 把 Claude 规划好的编码任务派给 GLM 落地（会直接改文件，进度实时转播进对话）
argument-hint: [任务描述]
---

用户想把这个编码任务派给 GLM 落地：

$ARGUMENTS

请这样做：

1. **规划是你的活，不是 GLM 的。** 方案没定就先想清楚；需要先摸代码就走 `/glm:explore`，别自己通读。

2. **派活前自检划算度：预期改动量是不是远大于任务描述量？**
   - ✅ 划算：大批量机械改动（几十个文件重命名、铺样板、成批补测试）、并行扇出、你的额度快到顶
   - ❌ 不划算：小改动、需要中途判断的活、自包含 spec 和 diff 差不多长

   不划算就直接跟用户说「这个更适合我自己改」，然后自己改。别硬派。

3. **把任务写成自包含的 spec**，结构照 `~/.codex/skills/dispatch-opencode-workers/references/task-spec.md`（Objective/Completion Boundaries/Locked Decisions/Required Reading/Allowed Scope/Work/Verification/Final Report）。GLM 是独立 OpenCode 进程看不到本对话，spec 必须自包含；写成文件（放 `/tmp/glm-task-xxx.md`），用 `--task-file` 传给 runner——任务走 stdin 进 worker，不在进程列表里。**验证命令选秒级的**（grep/diff/typecheck），重量级 `pnpm test` 留给主会话自己跑。通用纪律（汇报格式、脏工作区处理、自验要求、权限预告）**不用写**——runner 自动注入 `prompts/*.md`。项目外素材先 cp 进项目根再引用。

4. **主会话直接后台派活（不走 subagent——这样进度才能实时进对话）。** 同一轮里做两件事：

   a. Bash（`run_in_background: true`）：
      ```bash
      python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
        --provider glm --mode code \
        --workdir "$PWD" \
        --task-file /tmp/glm-task-xxx.md
      ```
      需要并行多个 code worker 时加 `--worktree`：各 worker 在独立 git worktree 里干活互不踩，路径在 `worker_runner.started` 事件里，核验合入后用 `python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py worktrees remove <路径>` 清掉。

   b. 立刻挂低噪 Monitor（只报错误与完成——每条事件都会唤起主会话烧一轮对话，
      全量转播已被用户判为低效；中间过程用户自己 tail 日志可见）：
      ```
      Monitor:
        command: tail -n 0 -f ~/.codex/opencode-workers/logs/glm/latest.log | grep -E --line-buffered '== 完成|!! '
        description: GLM worker 错误与完成信号
        timeout_ms: 3600000
        persistent: false
      ```
      常规事件通知不用回话；需要看中间过程时临时读日志文件，别放宽过滤器。
      看到连续多条 `!! `（权限拦截/报错）→ worker 大概率卡住了：读日志确认，必要时 TaskStop 杀掉处理，别干等。

5. **worker 失败/没收敛时优先 resume，不要冷启动重派**：
   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/dispatch-opencode-workers/scripts/run_worker.py" \
     --provider glm --mode resume \
     --session <summary 里的 session_id> \
     --workdir <summary 里的 workdir> \
     --task "上次任务没完成：<具体指出差什么、怎么修>"
   ```
   resume 复用 worker 上次会话的全部上下文，追问一句就能继续；
   冷启动重派要重读全部文件，分钟级往返。同一障碍撞两次还不过 → 你自己接手收尾。

6. **后台任务退出（收到完成通知）后**：TaskStop 停掉 Monitor，然后**你自己核验——这是唯一的一次核验，不能省**：
   - `git diff --stat` 对照 spec 白名单查越界（注意剔除会话开始前就存在的脏改动）
   - `git diff` 抽查实际改动是否符合方案
   - 跑测试/lint
   - **全量重写级的大 diff（数百行以上）核验前先派 `/kimi:review` 跨模型审一遍**（方向：正确性 + spec 符合度）——两个模型的错误不相关，GLM 的系统性盲点 Kimi 能看见，花的是便宜额度。小改动跳过这步。

7. **诚实汇报**：改了什么，核验发现什么问题，哪里需要用户自己看一眼。

并行扇出多个 worker 时不适合本流程（Monitor 事件会交错），改走 glm-coder subagent，一个实例管一个 worker。
任务描述太模糊没法派就先问用户，别硬派。
