# OpenCode Worker 共享地基 — 架构文档

> 2026-07-17 生效。本文档是 **Codex 和 Claude 两个宿主的共同契约**。
> 改动此目录下的任何文件前，必须读完本文档的"共享契约"章节。

## 1. 谁在用这套地基

```
Claude 主会话 ──────┐
  /glm:code 等       │       ┌──→ GLM  (glm-worker/glm-5.2)
  8 个 agent          ├──→ run_worker.py ──→ OpenCode 1.18.3
  8 个 command        │       └──→ Kimi (kimi-worker/k3)
Codex 主控 ──────────┘
  $glm / $kimi skills
```

**两个宿主共享同一个 runner**。任何改动两边同时生效——这是收益也是约束。

## 2. 目录结构

```
~/.codex/skills/dispatch-opencode-workers/
├── ARCHITECTURE.md          ← 本文档（共享契约）
├── SKILL.md                 ← Codex 侧调度说明
├── agents/openai.yaml       ← Codex 入口元数据
├── prompts/                 ← worker system prompt（单源）
│   ├── common.md            ← 所有模式共享
│   ├── code.md
│   ├── explore.md
│   ├── test.md
│   └── review.md
├── references/
│   ├── entry-flow.md        ← $glm/$kimi 共享解析/派活/验收流程
│   ├── provider-contract.md ← 路由、隔离、安全约束
│   └── task-spec.md         ← 任务契约模板
└── scripts/
    ├── providers.py         ← ★ 唯一事实源（Provider registry + 版本号）
    ├── policy.py            ← 权限矩阵 + config 生成 + prompt 加载
    ├── runtime.py           ← 环境/凭据/进程/超时/worktree
    ├── run_worker.py        ← 薄 CLI 编排
    ├── fmt_events.py        ← 实时日志渲染（便利层，故障不影响 worker）
    ├── configure_credentials.py
    ├── install_runtime.sh   ← 从 providers.py 读版本号安装
    ├── maintain.py          ← 日志清理 + worktree 生命周期
    └── tests/
        └── test_run_worker.py  ← 20 个自动化测试
```

Codex 侧入口：
```
~/.codex/skills/glm/SKILL.md      → --provider glm
~/.codex/skills/kimi/SKILL.md     → --provider kimi-k3
```

Claude 侧入口（不在本目录，仅列出供参照）：
```
~/.claude/agents/{glm,kimi}-{coder,explorer,reviewer,tester}.md   ← 8 个 agent
~/.claude/commands/{glm,kimi}/{code,explore,review,test}.md       ← 8 个 slash command
```

运行时数据：
```
~/.codex/opencode-workers/
├── providers/
│   ├── glm/        ← GLM 独立 XDG（config/data/state/cache/auth）
│   └── kimi-k3/    ← Kimi 独立 XDG
├── logs/
│   ├── glm/        ← GLM 日志（JSONL + rendered latest.log + archives）
│   └── kimi-k3/
└── worktrees/      ← detached worktrees
```

## 3. 共享契约 — 不能随便改的部分

以下规则两个宿主共同依赖，**改动任何一条必须同时满足全部验证条件**。

### 3.1 Provider 路由（锁死）

| 用户入口 | provider 参数 | OpenCode Provider ID | 模型 ID | 端点 |
|---|---|---|---|---|
| `$glm` / `/glm:*` | `glm` | `glm-worker` | `glm-5.2` | `https://open.bigmodel.cn/api/coding/paas/v4` |
| `$kimi` / `/kimi:*` | `kimi-k3` | `kimi-worker` | `k3` | `https://api.kimi.com/coding/v1` |

**唯一事实源**：`scripts/providers.py` 中的 `PROVIDERS` 字典。
**不允许**：任务覆盖 provider/model/endpoint/adapter；增加任意模型或 relay；静默 fallback 到另一个 provider。

### 3.2 OpenCode 版本（锁死）

**唯一事实源**：`scripts/providers.py` 中的 `PINNED_OPENCODE_VERSION`。
当前值：`1.18.3`。

- `install_runtime.sh` 从 `providers.py` 读取版本号（`python3 providers.py`），不硬编码。
- `run_worker.py` 每次运行前校验二进制版本，不一致直接失败。
- 升级 = 契约变更：改 `providers.py` 一处 → 重装 runtime → 跑全部测试 → 两宿主各冒烟一次。

### 3.3 凭据隔离（不可放松）

- 每个 provider 独立 XDG 目录，auth.json 权限 `0600`，目录 `0700`。
- Key 不进 CLI 参数、环境变量、任务契约、普通日志。
- 原子写入（tempfile + fsync + rename）。
- stdout/stderr 如果意外出现 key 则替换为 `[REDACTED]`。
- Key 只通过 HTTPS 发给选中的官方 provider，不经 relay。
- 不复用 Claude 的 key、XDG、session、config、skills。

### 3.4 权限矩阵（语义约束）

**OpenCode 1.18.3 的权限裁决是 last-match-wins**（`findLast`，插入顺序即优先级）。
这意味着 `policy.py` 中规则的**插入顺序是正确性属性**。

关键不变式：
- 只读三模式（explore/review/test）的 `*>*` deny **必须插在所有 allow 之后**，否则 `rg x > f` 会命中靠后的 `rg *`(allow) 而泄漏。
- code 模式的 deny 规则（`git push*`、`curl *`、`*auth.json*` 等）**必须插在 `*: allow` 之后**。
- 改动规则顺序后必须跑 `test_last_match_wins_resolution_matches_binary_semantics`。

**不用加的（死规则）**：`*|*`、`*&&*`、`*;*`、`*||*`、反引号、`*$(*`、`*<*`。OpenCode 用 tree-sitter 拆段匹配，每个命令节点单独过规则，运算符不出现在节点文本里。

### 3.5 进程隔离（不可放松）

- `--pure` + `--thinking` + `--format json`。
- 任务契约走 stdin，不进 argv。
- sharing disabled、autoupdate disabled、Claude Code 兼容加载全关、插件全关。
- worker 不能委派子代理、不能 MCP、不能 webfetch/websearch。
- 只读模式禁 edit/write。
- 环境变量白名单继承（`SAFE_ENV_KEYS`），不继承任何 `*API_KEY*`。

### 3.6 输出契约（不可改变格式）

- 第一行 stdout：`worker_runner.started`（JSON，含 run_id、日志路径）。
- 最后一行 stdout：`worker_runner.summary`（JSON，含 session_id、exit_code、timed_out/idle_timed_out/interrupted）。
- 两个宿主都解析这两个事件，格式变了两边同时坏。

### 3.7 日志契约

- 原始 JSONL（`<run_id>.jsonl` + `.stderr.log`）：权威记录，`maintain.py` 显式清理。
- 渲染 `latest.log`：便利层，`tail -f` 用。**渲染层故障不得影响 worker 执行和退出码**。
- Monitor grep 标记：`== 完成` 和 `!! ` —— Claude 侧 Monitor 直接 grep 这两个串。
- 存档保留 20 份/provider。

### 3.8 worker prompt（共享单源）

`prompts/*.md` 是 worker 的 system prompt。runner 在 `policy.build_config` 里读取并注入 agent 定义。

**两个宿主的调度纪律（划算度自检、spec 纪律、抽查、互审）不在这里**——那些分别存在各自的 agent/skill/command 定义里。prompts/ 只放 worker 内部的执行纪律。

## 4. 可以改但需要验证的部分

| 改什么 | 约束 |
|---|---|
| `prompts/*.md` 内容 | 保持报告格式 `STATUS/FILES_CHANGED/VALIDATION/REMAINING_RISKS` 不变 |
| `policy.py` 增加 allow 规则 | 加在正确位置（deny 之前）；跑权限测试 |
| `fmt_events.py` 渲染改进 | 保持 `== 完成` 和 `!! ` 标记不变（Monitor 依赖） |
| `maintain.py` 增加功能 | 保持"不静默删最新日志"和"dirty worktree 拒绝删除" |
| `runtime.py` 超时默认值 | 当前 timeout=3600s, idle=900s, heartbeat=60s |
| `references/*.md` 文档 | 保持与 `providers.py` 一致（防漂移测试会断言） |

## 5. 验证清单 — 任何改动后必须全过

```bash
# 1. 全部 20 个单测
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker

# 2. 双路 dry-run（字段与锁定路由一致）
python3 run_worker.py --provider glm --mode code --workdir /tmp --task x --dry-run
python3 run_worker.py --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 3. 版本单源验证
python3 providers.py  # 应输出 1.18.3（当前）

# 4. 凭据状态
python3 configure_credentials.py all --status
```

大改（provider 路由、版本升级、权限矩阵、输出格式）后额外：
```bash
# 5. 真实冒烟（消耗少量额度）
python3 run_worker.py --provider glm --mode explore --workdir <项目> --task "Read any file and report its first line."
python3 run_worker.py --provider kimi-k3 --mode explore --workdir <项目> --task "Read any file and report its first line."
```

## 6. Codex 侧需要同步更新的文件

当共享地基发生以下变更时，Codex 侧入口也需要同步：

| 地基变更 | 需要同步的 Codex 文件 |
|---|---|
| 新增/删除模式（如增加 `deploy`） | `glm/SKILL.md`、`kimi/SKILL.md`、`references/entry-flow.md` |
| 变更 CLI 参数 | `glm/SKILL.md`、`kimi/SKILL.md`、`references/entry-flow.md` |
| 变更日志路径 | `SKILL.md`、`references/entry-flow.md` |
| 变更报告格式 | `prompts/*.md`、`references/task-spec.md` |

**Claude 侧的 8 个 agent + 8 个 command 不归 Codex 管**，但如果变更了共享接口（CLI 参数、summary 格式、日志路径），需要通知用户同步 Claude 侧。

## 7. 安装/升级步骤

### 首次安装

```bash
# 1. 安装固定版本 OpenCode runtime
bash ~/.codex/skills/dispatch-opencode-workers/scripts/install_runtime.sh

# 2. 配置凭据（交互式，key 不显示）
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py glm
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py kimi-k3

# 3. 验证
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/configure_credentials.py all --status
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/run_worker.py \
  --provider glm --mode code --workdir /tmp --task x --dry-run
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/run_worker.py \
  --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 4. 跑测试
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker
```

### 升级 OpenCode 版本

```bash
# 1. 改唯一事实源
# 编辑 ~/.codex/skills/dispatch-opencode-workers/scripts/providers.py
# 修改 PINNED_OPENCODE_VERSION = "新版本号"

# 2. 重装 runtime
bash ~/.codex/skills/dispatch-opencode-workers/scripts/install_runtime.sh

# 3. 跑全部测试
cd ~/.codex/skills/dispatch-opencode-workers/scripts
python3 -m unittest tests.test_run_worker

# 4. 双路 dry-run
python3 run_worker.py --provider glm --mode code --workdir /tmp --task x --dry-run
python3 run_worker.py --provider kimi-k3 --mode code --workdir /tmp --task x --dry-run

# 5. 真实冒烟（两个 provider 各跑一次小任务）
python3 run_worker.py --provider glm --mode explore --workdir <项目路径> \
  --task "Read any file and report its first line."

# 6. 通知用户同步 Claude 侧冒烟（用户自行在 Claude Code 里跑 /glm:explore 验证）
```

### 日志清理

```bash
# 删除 14 天前的日志（永不删各 provider 最新一次运行日志）
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  logs --older-than-days 14

# 只清理 GLM
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  logs --older-than-days 14 --provider glm
```

### Worktree 管理

```bash
# 列出所有 worker worktree
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py worktrees list

# 删除一个已集成的干净 worktree（dirty 或有未集成 commit 会拒绝）
python3 ~/.codex/skills/dispatch-opencode-workers/scripts/maintain.py \
  worktrees remove /absolute/path/to/worktree
```

## 8. 安全边界声明

当前权限模型（OpenCode 工具权限 + shell deny 规则 + 凭据隔离 + XDG 分离）是**防失误不防恶意**。它防的是模型手滑 `git push`、顺手 `curl` 外传、读错凭据文件。

它防不了：
- 恶意仓库里的提示注入（`python3 -c "urllib.request.urlopen(...)"` 不含 `curl` 字样，bypass 全部 shell 规则）
- worker 自己就有 OS 用户权限读文件

恶意仓库场景需要 OS/容器沙箱，不是这层字符串规则能解决的。不要把当前隔离宣传为安全沙箱。
