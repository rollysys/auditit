# auditit

**Claude Code 会话实时审计监控工具。**

在 tmux 旁观窗格中实时展示任意 Claude Code 会话的完整执行过程 —— 提示词、工具调用、工具结果、助手输出、Token 用量、上下文压力、成本，以及**完整的 sub-agent 调用树** —— 全程不修改你的全局 `~/.claude/settings.json`。

```
┌──────────────────────┬──────────────────────────────┐
│                      │  AUDIT (live tree view)      │
│  AUDITOR             │  ● session  sonnet           │
│  (你的 claude，      │  💰 $0.042  📊 42% ctx       │
│   不被审计，         │                              │
│   用于查看日志、     │  14:32:48  👤 重构 parser... │
│   复盘、提问)        │  14:32:51  📖 Read ...       │
│                      │  14:32:52  ┌ 🤖 SUBAGENT    │
│                      │  14:32:53  │  🔍 Grep ...   │
│                      │  14:32:55  └ ✔ turns=2      │
│                      ├──────────────────────────────┤
│                      │  WORKER (被审计的 claude)    │
│                      │  AUDITIT_TARGET=1 claude ... │
└──────────────────────┴──────────────────────────────┘
```

---

## 为什么需要它

Claude Code 提供了 hook 机制，每次工具调用都会触发，这本来是做审计的理想切入点。但**把 hook 写到全局 `~/.claude/settings.json` 有两个致命问题**：

1. 你用来**查看审计日志**的那个 claude 进程也会被捕获，日志里全是自己读日志的记录，互相污染。
2. 任何开着的后台 claude 进程都会被动写入 —— 你不想审计的会话也被打扰。

`auditit` 用两层隔离保证**只有你想审计的那一个进程会写日志**：

1. **Per-session settings**：每次启动生成一个会话专属 `settings.json`，通过 `claude --settings <path>` 传给被审计进程。全局配置零改动。
2. **`AUDITIT_TARGET=1` 环境变量门禁**：`audit_hook.sh` 第一行检查该变量，没设就立即 `exit 0`。即便 hook 被意外加载，也不会产生任何输出。

Sub-agent（通过 Agent 工具派生的子进程）**自动继承**父进程的 `--settings` 和环境变量，因此整棵 agent 调用树无论嵌套多深都会被完整捕获，而审计器自身的 claude 永远在门外。

### `--command` 模式：审计任意脚本

当你的脚本会 fork 多个独立 claude 进程时，`--settings` CLI 参数无法自动继承。`--command` 模式通过 `CLAUDE_CONFIG_DIR` 解决：

1. 创建临时 config 目录，将 `~/.claude/*` 全部 symlink 过来，**只替换 `settings.json`** 为带 audit hooks 的版本
2. `export CLAUDE_CONFIG_DIR=<临时目录>` —— 所有子进程自动继承，加载带 hooks 的 settings
3. 配合 `AUDITIT_TARGET=1` 门禁，脚本内任意深度的 claude 进程都写入同一份 `audit.jsonl`

```
CLAUDE_CONFIG_DIR=.../config  ← 子进程继承，加载带 audit hooks 的 settings
AUDITIT_TARGET=1              ← 子进程继承，hook 允许写日志
你的脚本
├── claude ...                ← 自动被捕获
├── claude ...                ← 自动被捕获
└── 其他非 claude 进程         ← 无影响
```

---

## 核心数据源

所有数据来自 Claude Code 官方 hook 事件，无任何 LLM 推断或采样：

| Hook 事件 | 采集内容 |
|---|---|
| `SessionStart` | `session_id`、模型、`cwd` |
| `UserPromptSubmit` | 完整用户输入 |
| `PreToolUse` / `PostToolUse` | 工具名、入参、返回、`agent_id` |
| `Stop` | 助手最终消息、token 统计、cost |
| `SessionEnd` | 总 token / cost / 轮次 / 耗时 |
| `SubagentStart` / `SubagentStop` | 父子 `agent_id`、最终输出、transcript 路径 |
| `Notification` | 系统通知 |

`agent_id` 是串联整棵调用树的关键字段；display 按嵌套深度缩进渲染，直观看到每个 sub-agent 的边界、耗时、token 与成本。

---

## 前置依赖

- `tmux` ≥ 3.0
- `python3` + `rich`（`pip install rich`）
- `claude` CLI（已登录）

缺任一项会直接报错退出，不会自动安装。

---

## 安装

作为 Claude Code skill：

```bash
git clone https://github.com/rollysys/auditit.git ~/.claude/skills/auditit
```

或者 clone 到任意位置，直接调用 `scripts/workbench.sh`。

---

## 快速开始

```bash
SKILL_DIR="$HOME/.claude/skills/auditit/scripts"

# 场景 1：托管 worker —— auditit 自动起一个被审计的 claude
bash "$SKILL_DIR/workbench.sh" start --model sonnet --repo-root /path/to/project

# 场景 2：审计任意命令 —— 命令派生的所有 claude 实例都会被捕获
bash "$SKILL_DIR/workbench.sh" start --command 'bash my_multi_agent_script.sh'

# 场景 3：手动模式 —— auditit 只开审计窗格，打印命令让你自己起
bash "$SKILL_DIR/workbench.sh" start
#   → AUDITIT_TARGET=1 claude --settings /tmp/auditit/session-xxx/settings.json

# 场景 4：headless 一次性任务
bash "$SKILL_DIR/workbench.sh" launch --prompt '分析 parser.cpp 的错误处理' --model sonnet

# 指定自定义 API endpoint
bash "$SKILL_DIR/workbench.sh" start --model sonnet --base-url https://my-proxy.example.com/v1

# 结束 + 生成报告
bash "$SKILL_DIR/workbench.sh" stop
bash "$SKILL_DIR/workbench.sh" report
```

### 运行行为

- **在 tmux 外调用**：创建新 tmux session `audit-<timestamp>`，布局 3 个窗格并 attach。
- **在 tmux 内调用**：在当前 session 新开一个 window，布局 3 个窗格并切换焦点。
- **省略 `--model`**：只开 auditor + audit display 两个窗格，worker 由你手动启动。

---

## 命令

| 命令 | 说明 |
|---|---|
| `start [--model MODEL ❘ --command CMD] [--base-url URL] [--repo-root DIR]` | 打开 3 窗格审计布局；`--model` 启动单个 claude worker；`--command` 运行任意命令，所有子 claude 进程通过 `CLAUDE_CONFIG_DIR` 自动被审计 |
| `launch --prompt STR [--model MODEL] [--base-url URL] [--max-turns N]` | 以 `claude -p` headless 模式运行，同时显示审计窗格 |
| `stop` | 关闭 audit display + worker 窗格（auditor 窗格保留） |
| `replay [audit.jsonl]` | 离线回放历史审计日志 |
| `status` | 查看当前会话状态（session dir、pane id 等） |
| `report [--session-dir DIR]` | 从 `audit.jsonl` 生成 Markdown 分析报告 |

### 按 session 过滤显示

当 `audit.jsonl` 意外混入了多个会话的事件，可用 `--session-id` 前缀匹配过滤：

```bash
python3 scripts/display.py --replay audit.jsonl --session-id 1fa96f8c
```

---

## 会话数据

所有数据落盘在 `/tmp/auditit/`（可用 `AUDITIT_DIR` 覆盖）：

```
/tmp/auditit/
├── current.state                # 当前活跃会话状态（session dir + pane id）
└── session-20260405-143652/
    ├── audit.jsonl              # 所有 hook 事件（append-only JSONL）
    ├── settings.json            # 本次会话专属的带 audit hooks 的 settings
    └── report.md                # 收尾生成的分析报告
```

`audit.jsonl` 是唯一的事实来源，display、replay、report 全部从它派生。

---

## 项目结构

```
auditit/
├── SKILL.md                    # Claude Code skill manifest
├── design.md                   # 详细设计文档（中文）
├── hooks/
│   └── audit_hook.sh           # AUDITIT_TARGET 门禁 → 写 JSONL
└── scripts/
    ├── workbench.sh            # 入口：tmux 编排（start/stop/launch/replay/report/status）
    ├── gen_settings.py         # 生成会话专属 settings.json（合并用户全局配置 + audit hooks）
    ├── display.py              # 实时树状渲染（sub-agent 嵌套、cost、ctx pressure）
    ├── render_events.py        # 事件到终端行的渲染层
    ├── worker.py               # 托管 worker 进程管理
    ├── analyzer.py             # 收尾报告生成器
    └── util.py                 # 公共工具
```

---

## 设计原则

- **被动只读**：audit 窗格绝不对 worker 进程产生任何副作用，只消费 hook stdin。
- **进程隔离**：全局 settings 零修改，双层门禁（per-session settings + env var）。
- **Fail fast**：依赖缺失直接报错退出，不自动修复。
- **单一数据源**：所有视图（live / replay / report）都从同一份 `audit.jsonl` 派生，可复现。

更多架构细节见 [`design.md`](design.md)。

---

## License

MIT. See [LICENSE](LICENSE).
