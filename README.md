# auditit

**Claude Code 会话实时审计监控工具。**

在 tmux 旁观窗格中实时展示任意 Claude Code 会话的完整执行过程 —— 提示词、工具调用、工具结果、助手输出、Token 用量、上下文压力、成本，以及**完整的 sub-agent 调用树** —— 全程不修改你的全局 `~/.claude/settings.json`。

```
┌──────────────────────┬──────────────────────────────┐
│                      │  ═══ AUDITIT ═══             │
│  AUDITOR             │  mode=model │ model=sonnet   │
│  (你的 claude，      │  ───────────────              │
│   不被审计，         │  14:32:48  ⚙ SESSION START   │
│   用于查看日志、     │  14:32:48  👤 重构 parser... │
│   复盘、提问)        │  14:32:51  📖 Read ...       │
│                      │  14:32:52  ┌ 🤖 SUBAGENT    │
│                      │  14:32:53  │  🔍 Grep ...   │
│                      │  14:32:55  └ ✔ turns=2      │
│                      ├──────────────────────────────┤
│                      │  WORKER (sonnet)             │
│                      │  AUDITIT_TARGET=1 claude ... │
└──────────────────────┴──────────────────────────────┘
```

---

## 两种审计后端

auditit 支持两种数据采集方式，产出**格式相同的 `audit.jsonl`**，display / replay / report 通用：

### hooks 后端（默认）

通过 `settings.json` 注入 hook 脚本，每次工具调用触发 `audit_hook.sh` 写入 JSONL。

- **注入方式**：`--settings`（单进程）或 `CLAUDE_CONFIG_DIR`（多进程）
- **进程隔离**：`AUDITIT_TARGET=1` 环境变量门禁
- **sub-agent 树**：完整（SubagentStart / SubagentStop 事件）
- **限制**：`--bare` 模式下 hooks 被跳过，无法使用

| Hook 事件 | 采集内容 |
|---|---|
| `SessionStart` | `session_id`、模型、`cwd` |
| `UserPromptSubmit` | 完整用户输入 |
| `PreToolUse` / `PostToolUse` | 工具名、入参、返回、`agent_id` |
| `Stop` | 助手最终消息、token 统计、cost |
| `SessionEnd` | 总 token / cost / 轮次 / 耗时 |
| `SubagentStart` / `SubagentStop` | 父子 `agent_id`、最终输出、transcript 路径 |

### stream-json 后端（bare 模式）

通过 `claude --output-format stream-json --verbose` 的 stdout 输出，由 `stream_to_audit.py` 转写为 audit.jsonl 格式。

- **注入方式**：PATH wrapper 自动为所有 `claude` 命令附加参数（用户可 override）
- **进程隔离**：`CLAUDE_CODE_SIMPLE=1` 环境变量继承
- **sub-agent 树**：无（单进程视角，bare 模式不派生 sub-agent）
- **适用场景**：需要最轻量启动（跳过 hooks / skills / plugins / memory 等）

---

## 进程隔离

### hooks 模式

两层隔离保证**只有你想审计的进程会写日志**：

1. **Per-session settings**：`gen_settings.py` 生成仅含 audit hooks 的 `settings.json`（不继承全局 settings），通过 `--settings` 作为 flagSettings 加载。Claude Code 运行时合并所有 settings source（flag / user / project / local），hooks 不会重复。
2. **`AUDITIT_TARGET=1` 门禁**：`audit_hook.sh` 检查该变量，没设就 `exit 0`。审计器自身的 claude 永远不写日志。

### 多进程覆盖（`--command` 模式）

`--settings` 是 CLI 参数，无法通过环境变量继承。`--command` 模式通过 `CLAUDE_CONFIG_DIR` 解决：

1. 创建临时 config 目录，将 `~/.claude/*` 全部 symlink，**只替换 `settings.json`**
2. `export CLAUDE_CONFIG_DIR=<临时目录>` —— 子进程自动继承
3. 配合 `AUDITIT_TARGET=1`，脚本内任意深度的 claude 进程都写入同一份 `audit.jsonl`

### bare 模式

`--bare` 设置 `CLAUDE_CODE_SIMPLE=1`，跳过 hooks / skills / plugins / memory / LSP / GrowthBook 等约 30 项功能。审计通过 stream-json 后端实现。多进程场景使用 PATH wrapper 自动注入 `--output-format stream-json --verbose`，用户显式传参可 override（commander 后者覆盖前者）。

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

## 启动模式

### `start`（交互式 tmux 布局）

```
布局: AUDITOR │ AUDIT + WORKER
结束: 手动 stop
```

```bash
SKILL_DIR="$HOME/.claude/skills/auditit/scripts"

# 单个 claude worker
bash "$SKILL_DIR/workbench.sh" start --model sonnet

# 任意命令（子 claude 自动被审计）
bash "$SKILL_DIR/workbench.sh" start --command 'bash my_script.sh'

# 任意命令 + 指定 sub-agent 默认模型
bash "$SKILL_DIR/workbench.sh" start --command 'bash my_script.sh' --model kimi-k2.5

# 手动模式（打印 hint，用户自己起 claude）
bash "$SKILL_DIR/workbench.sh" start

# 指定自定义 API endpoint
bash "$SKILL_DIR/workbench.sh" start --model sonnet --base-url https://my-proxy.example.com/v1
```

### `launch`（headless 后台任务）

```
布局: 原 pane │ AUDIT
结束: SessionEnd 自动退出
```

```bash
# 单次任务（hooks 审计）
bash "$SKILL_DIR/workbench.sh" launch --prompt '分析错误处理' --model sonnet

# 单次任务（bare 模式，stream-json 审计）
bash "$SKILL_DIR/workbench.sh" launch --prompt '分析错误处理' --model sonnet --bare

# 任意命令（hooks 审计，CLAUDE_CONFIG_DIR）
bash "$SKILL_DIR/workbench.sh" launch --command 'bash my_script.sh'

# 任意命令 + bare（stream-json，PATH wrapper）
bash "$SKILL_DIR/workbench.sh" launch --command 'bash my_script.sh' --bare
```

### `replay`（离线回放）

两种后端产出的 `audit.jsonl` 格式相同，replay 通用。

```bash
bash "$SKILL_DIR/workbench.sh" replay /path/to/audit.jsonl
```

### 其他命令

```bash
bash "$SKILL_DIR/workbench.sh" stop                    # 关闭 audit + worker 窗格
bash "$SKILL_DIR/workbench.sh" status                  # 查看当前会话状态
bash "$SKILL_DIR/workbench.sh" report                  # 生成 Markdown 分析报告
```

---

## model 配置

| 参数 | 作用 | 影响范围 |
|---|---|---|
| `--model MODEL` | claude CLI 的 `--model` 参数 | 主进程 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | sub-agent 默认 sonnet 模型 | 全进程树（自动注入） |
| `--base-url URL` | `ANTHROPIC_BASE_URL` 环境变量 | 全进程树（自动注入） |

`--model` 在 `--command` 模式下仅注入 `ANTHROPIC_DEFAULT_SONNET_MODEL` 环境变量，不控制主进程启动命令。

---

## 环境变量注入矩阵

| 变量 | start --model | start --command | launch --prompt | launch --bare | launch --command | launch --command --bare |
|---|---|---|---|---|---|---|
| `AUDITIT_TARGET=1` | ✓ | ✓ | ✓ | — | ✓ | — |
| `CLAUDE_CONFIG_DIR` | — | ✓ | — | — | ✓ | — |
| `CLAUDE_CODE_SIMPLE=1` | — | — | — | ✓ | — | ✓ |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | ✓ | 可选 | ✓ | ✓ | 可选 | 可选 |
| `ANTHROPIC_BASE_URL` | 可选 | 可选 | 可选 | 可选 | 可选 | 可选 |
| `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `--settings` | ✓ | — | ✓ | — | — | — |
| PATH wrapper | — | — | — | — | — | ✓ |

---

## Audit 面板

audit pane 启动时首先显示本次审计参数：

```
═══ AUDITIT ═══
  mode=bare/stream-json  │  model=kimi-k2.5  │  max_turns=50  │  bare
  api=https://my-proxy.example.com/v1  │  cwd=/Users/x/myproject
  prompt=分析 parser.cpp 的错误处理逻辑
───────────────
```

随后实时渲染事件流，sub-agent 按嵌套深度缩进：

```
14:32:48  ⚙  SESSION START  abc123
14:32:48  👤 USER  │  重构 parser.cpp 的错误处理
14:32:51  📖 Read  src/parser.cpp  →  ✔  234 lines
14:32:52  ┌ 🤖 SUBAGENT  depth=1  id=a1b2c3d4
14:32:53  │  🔍 Grep  "TODO"  →  ✔  5 matches
14:32:55  └ 🤖 SUBAGENT STOP  ✔  turns=2  $0.0018
14:32:56  🏁 STOP  │  重构完成
         💰 $0.0312  │  tools:6  │  sub-agents:1
📊 SUMMARY  │  turns=4  │  tools=6  │  ctx=38%  │  api=https://...
```

### 按 session 过滤

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
    ├── audit.jsonl              # 所有审计事件（append-only JSONL）
    ├── settings.json            # 本次会话 audit hooks（仅 hooks 模式）
    ├── config/                  # CLAUDE_CONFIG_DIR symlink 目录（仅 --command 模式）
    ├── prompt.txt               # 用户 prompt（仅 launch 模式）
    ├── output.txt               # claude 输出
    ├── stderr.txt               # stderr 输出（仅 bare 模式）
    └── report.md                # 收尾生成的分析报告
```

`audit.jsonl` 是唯一的事实来源，display、replay、report 全部从它派生。

---

## 项目结构

```
auditit/
├── SKILL.md                     # Claude Code skill manifest
├── design.md                    # 详细设计文档（中文）
├── hooks/
│   └── audit_hook.sh            # AUDITIT_TARGET 门禁 → 写 JSONL + 采集 ANTHROPIC_BASE_URL
└── scripts/
    ├── workbench.sh             # 入口：tmux 编排（start/stop/launch/replay/report/status）
    ├── gen_settings.py          # 生成仅含 audit hooks 的 settings.json（不继承全局配置）
    ├── display.py               # 实时树状渲染（AuditMeta / sub-agent 嵌套 / cost / ctx pressure）
    ├── stream_to_audit.py       # stream-json → audit.jsonl 转换器（bare 模式）
    ├── render_events.py         # 事件到终端行的渲染层
    ├── worker.py                # 托管 worker 进程管理
    ├── analyzer.py              # 收尾报告生成器
    └── util.py                  # 公共工具
```

---

## 设计原则

- **被动只读**：audit 窗格绝不对 worker 进程产生任何副作用。
- **进程隔离**：全局 settings 零修改，双层门禁（per-session settings + env var）。
- **双后端统一**：hooks 和 stream-json 产出相同格式的 `audit.jsonl`，上层通用。
- **Fail fast**：依赖缺失直接报错退出，不自动修复。
- **单一数据源**：所有视图（live / replay / report）都从同一份 `audit.jsonl` 派生，可复现。
- **可 override**：`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` 覆盖远程下发的 Read 限制；PATH wrapper 的参数可被用户显式传参覆盖。

更多架构细节见 [`design.md`](design.md)。

---

## License

MIT. See [LICENSE](LICENSE).
