# auditit: Claude Code Session Audit Monitor — Design

## 1. 一句话定义

**auditit** 是一个通用 Claude Code 会话审计工具：在 tmux 旁观窗格中实时展示任意 Claude Code 会话的完整执行过程——提示词、工具调用、工具结果、助手输出、Token 用量、上下文压力——供用户事后优化 agent 质量与成本。

---

## 2. 核心场景

```
tmux window
┌──────────────────────────────────┬─────────────────────────────┐
│  Worker pane                     │  Audit pane (本 skill)      │
│  $ claude                        │                             │
│  > 重构 parser.cpp 的错误处理    │  ● SESSION  sonnet          │
│                                  │  💰 $0.042  📊 42% ctx      │
│  (Claude 在此处理任务)           │                             │
│                                  │  15:32:42 👤 重构 parser... │
│                                  │  15:32:45 📖 Read parser.cpp│
│                                  │  15:32:46 🔍 Grep error_han │
│                                  │  15:32:48 💬 发现3处可优化  │
│                                  │  15:32:50 ✏️  Edit parser.cp│
│                                  │  15:32:52 🔧 Bash: make test│
│                                  │  15:32:54 🏁 已完成，测试通过│
│                                  │                             │
│                                  │  ─── 工具统计 ───           │
│                                  │  Read×1 Grep×1 Edit×1 Bash×1│
│                                  │  轮次:4  失败:0  耗时:13s   │
└──────────────────────────────────┴─────────────────────────────┘
```

Audit pane 是**只读旁观者**，不干预 Worker pane 的任何操作。

---

## 3. 数据采集机制

### 3.1 核心数据源：Claude Code Hooks

Claude Code 在 `settings.json` 中注册 hook 脚本，以下事件触发时将完整数据写入 hook stdin：

| Hook 事件 | 包含内容 | 审计价值 |
|-----------|----------|----------|
| `SessionStart` | `session_id`、模型名、`cwd` | 标记会话开始，建立 session→agent 映射 |
| `UserPromptSubmit` | 完整用户输入 | 完整提示词 |
| `PreToolUse` | `tool_name`、`tool_input`、**`agent_id`** | 工具调用意图，可归属到具体 agent |
| `PostToolUse` | `tool_name`、`tool_response`、**`agent_id`** | 工具执行结果 |
| `PostToolUseFailure` | `tool_name`、错误信息、**`agent_id`** | 失败链路 |
| `Stop` | 最终助手消息、token 统计、**`agent_id`** | 单个 agent 输出质量 + 成本 |
| `SessionEnd` | 总 token、cost、轮次、耗时 | 整个会话完整成本核算 |
| `SubagentStart` | **`agent_id`**（子）、`session_id`（父）、`cwd` | 子 agent 启动，建立父子关系 |
| `SubagentStop` | **`agent_id``**、`last_assistant_message`、`reason`、`agent_transcript_path` | 子 agent 完成，含最终输出 |
| `Notification` | 通知内容 | 系统状态 |

> **`agent_id` 是串联整棵 agent 树的关键字段**。每个 hook 事件均携带 `agent_id`，通过 `SubagentStart` 的父子 `session_id` 关系可重建完整调用树。

### 3.2 Sub-agent 完整覆盖策略

Sub-agent 是独立 Claude 进程，**不自动继承父会话的 `--settings` 参数**。要让 hooks 覆盖整棵 agent 树，有两种可靠方式：

#### 方案一：全局 hooks（推荐）

将 audit hooks 写入 `~/.claude/settings.json`（全局配置），所有 Claude 进程（含任意深度的 sub-agent）启动时都会加载该文件，自动被捕获。

auditit 在 `workbench.sh start` 时：
1. 读取现有 `~/.claude/settings.json`（如存在）
2. **合并**（不覆盖）加入 audit hooks，备份原文件
3. audit pane 启动，开始监听
4. `workbench.sh stop` 时恢复原 settings.json

```
~/.claude/settings.json  ← auditit 临时注入 hooks
        ↓ 被所有进程加载
  parent session
  └── sub-agent A      ← 同样加载全局 settings，hooks 生效
      └── sub-agent B  ← 同上
```

#### 方案二：--settings 透传（headless Mode B 专用）

`claude -p --settings` 参数是否传递给 sub-agent 取决于 Claude Code 内部实现（目前行为不确定）。故 Mode B 也同时启用全局 hooks 作为保底。

### 3.3 Agent 树重建

audit.jsonl 中所有事件都携带 `agent_id`。重建树的算法：

```
root_session_id  ← SessionStart 中 session_id（且无对应 SubagentStart）
每个 SubagentStart:
    child_agent_id  → 写入事件的 agent_id
    parent 通过 session_id 关联（SubagentStart 的 session_id = 父 agent 的 session_id）

agent_tree[agent_id] = {
    parent_agent_id,
    depth,           # 0 = root
    events: []       # 按 ts 排序的该 agent 所有事件
}
```

### 3.4 辅助数据源：stream-json（可选，headless 模式）

`claude -p --output-format stream-json --verbose` 时，sub-agent 工具调用**内联出现在父 stream** 中（`--verbose` 保证透明），额外提供：
- 思考过程（thinking blocks）
- 每轮增量 token 变化（无需等待 Stop 事件）

两路数据以 `tool_use_id` 为键合并，去重后进入统一事件流。

### 3.5 上下文压力计算

每个 agent 独立计算：
```
ctx_pressure(agent) = (input_tokens + cache_read_tokens) / model_context_window
```

模型上下文窗口在 `util.py` 中维护静态映射表（`claude-sonnet-4-6` → 200k 等）。

---

## 4. 架构

```
auditit/
├── SKILL.md                    # Claude Code skill 定义与调用说明
├── design.md                   # 本文档
│
├── scripts/
│   ├── workbench.sh            # 入口：tmux pane 编排 + 全局 settings 注入/恢复
│   ├── display.py              # 实时展示引擎（tail audit.jsonl → rich 渲染，含树状嵌套）
│   ├── worker.py               # Headless claude -p 子进程管理 + stream-json 渲染
│   ├── render_events.py        # 事件解析与树状终端渲染（stream-json + audit JSONL）
│   ├── gen_settings.py         # 合并 audit hooks 到现有 settings.json
│   ├── analyzer.py             # 会话结束后：agent 树重建 + 成本分解 + 质量报告
│   └── util.py                 # 公共工具（JSON、时间、agent 树、模型 ctx 映射）
│
└── hooks/
    └── audit_hook.sh           # Claude CLI hook 脚本（全事件 → JSONL，含 agent_id）
```

### 关键流程

```
workbench.sh start
    │
    ├─ 生成 SESSION_DIR（时间戳目录）
    ├─ 备份 ~/.claude/settings.json → settings.json.bak
    ├─ 调用 gen_settings.py → 合并 audit hooks 到 ~/.claude/settings.json
    │      （所有后续 claude 进程，含 sub-agents，均自动被 hook 覆盖）
    ├─ 在 tmux 中打开 audit pane
    │      └─ python display.py --follow SESSION_DIR/audit.jsonl
    │
    └─ 输出提示：
           "Audit pane 已就绪，直接运行 claude 即可（hooks 已全局注入）"

workbench.sh stop
    │
    └─ 恢复 ~/.claude/settings.json（从 bak）
```

---

## 5. Audit Pane 展示设计

### 5.1 顶部状态栏（常驻）

```
● SESSION  claude-sonnet-4-6  │  💰 $0.042  │  📊 42% ctx  │  🔄 轮次: 4  │  ⏱  13s
```

- `●` 颜色：绿色=运行中，黄色=等待，灰色=已结束
- 成本：累计实时更新（`Stop` 事件触发更新）
- ctx：上下文压力百分比，超过 70% 变黄，超过 85% 变红
- 轮次、耗时：实时累计

### 5.2 事件流（主体）：支持多层 sub-agent 嵌套

事件按时间顺序滚动，**sub-agent 的事件以缩进表示层级**：

```
15:32:41  ⚙  SESSION START     │  sonnet  [root]
15:32:42  👤 USER               │  重构 parser.cpp 的错误处理逻辑
15:32:45  📖 READ               │  src/parser.cpp  →  ✔  234 lines
15:32:46  🔍 GREP               │  "error_handler"  →  ✔  3 matches
15:32:47  💬 ASSISTANT          │  需要深入分析，委派子 agent 处理...
15:32:48  ┌ 🤖 AGENT  depth=1  │  agent_id=a1b2  "分析错误处理模式"
15:32:49  │  📖 READ            │    src/error.h  →  ✔  88 lines
15:32:50  │  🔍 GREP            │    "throw "  →  ✔  12 matches
15:32:51  │  💬 ASSISTANT       │    发现3种错误处理模式...
15:32:52  │  ┌ 🤖 AGENT depth=2 │    agent_id=c3d4  "查找相关测试"
15:32:53  │  │  🔍 GREP         │      "TEST.*error"  →  ✔  5 matches
15:32:54  │  └ 🤖 STOP  depth=2 │    ✔  找到5个测试用例  turns=2
15:32:55  └ 🤖 STOP   depth=1  │  ✔  分析完成  turns=4  $0.008
15:32:56  ✏️  EDIT               │  src/parser.cpp  →  ✔
15:32:58  🔧 BASH               │  make test  →  ✔  exit=0
15:33:00  🏁 STOP  [root]       │  重构完成，测试通过  turns=6  $0.031
```

**缩进规则**：
- 每层 sub-agent 缩进 2 个空格 + `│` 竖线前缀
- `┌` 标记 SubagentStart，`└` 标记 SubagentStop
- 最大展示深度 configurable（默认 5 层），超出折叠为 `[+N deeper]`

**显示规则**：
- 工具入参：取最有意义字段（路径 / 命令 / 搜索词），截断至 60 字符
- 工具出参：状态（✔/✘）+ 关键摘要（行数 / exit code / match count）
- Assistant 文本：取前 100 字符
- 失败事件：整行红色高亮
- Sub-agent Stop：显示该 agent 的 turns + 独立成本

### 5.3 底部工具统计栏（动态更新）

```
─── 工具统计（全树）──────────────────────────────────────────────────
Read×5  Grep×4  Edit×2  Bash×1  Agent×2  │  失败: Bash×1  │  总调用: 14
sub-agents: 2  (depth_max=2)             │  子 agent 成本: $0.016
```

统计覆盖整棵 agent 树（所有 `agent_id` 的 PreToolUse 合计）。

### 5.4 会话结束后：Summary 面板

`SessionEnd` 事件触发后，在事件流下方追加：

```
════════════════════════════════════════════════
  AUDIT SUMMARY
  耗时: 43s    轮次: 6（root）    sub-agents: 2
  总工具调用: 14（root: 8 + agents: 6）    失败率: 7%

  成本分解:
    root         $0.0232   ctx峰值 38%
    agent a1b2   $0.0062   ctx峰值 22%  (depth=1)
    agent c3d4   $0.0018   ctx峰值  9%  (depth=2)
    ─────────────────────────────
    总计         $0.0312

  Token:
    input(新)=6,120  cache_hit=12,300(67%)  output=1,204

  ⚠ 注意点:
    · Bash 调用失败 1 次（exit≠0）
    · sub-agent c3d4 仅 2 轮，成本极低，委派合理
    · cache_hit 率高 (67%)，提示词结构良好
════════════════════════════════════════════════
```

---

## 6. 工作模式

### Mode A：Watch（推荐，交互式 claude）

用户自己的 `claude` 会话使用生成的 settings.json，audit pane 被动监听。

```bash
# 启动 audit pane
bash workbench.sh start

# 系统输出：
# [auditit] 审计会话目录: /tmp/auditit/session-20260403-152300
# [auditit] Audit pane 已就绪，正在等待事件...
# [auditit] 在目标 pane 中运行：
#     claude --settings /tmp/auditit/session-20260403-152300/settings.json

# 用户在 worker pane 中运行 claude，audit pane 自动显示
```

### Mode B：Launch（headless `claude -p`）

auditit 直接启动 `claude -p`，同时监听 stream-json + hooks：

```bash
bash workbench.sh launch \
  --prompt "重构 src/parser.cpp 的错误处理" \
  --repo-root /path/to/repo \
  --model sonnet \
  --max-turns 50
```

两个 pane：左侧 Worker（claude -p 输出），右侧 Audit（实时树）。

### Mode C：Replay（离线回放）

对已有 audit.jsonl 做离线回放分析：

```bash
python display.py --replay /path/to/audit.jsonl
python analyzer.py --session /path/to/session-dir
```

---

## 7. 会话数据目录

```
/tmp/auditit/
└── session-20260403-152300/
    ├── settings.json          # 本次会话用的 claude settings（含 hook 注册）
    ├── hooks/
    │   └── audit_hook.sh      # 本次 hook 脚本（路径绝对化后的副本）
    ├── audit.jsonl            # 所有 hook 事件流（append-only）
    ├── stream.jsonl           # stream-json 输出（Mode B）
    └── report.md              # 会话结束后生成的分析报告
```

默认写在 `/tmp/auditit/`，可通过 `--session-dir` 覆盖。

---

## 8. 分析报告（analyzer.py）

会话结束后（或手动触发）生成 `report.md`，包含：

### 8.1 Agent 树成本分解

按 agent 层级展示成本分布，识别哪个 sub-agent 代价异常：

| Agent | Depth | Turns | Cost | ctx峰值 | 工具调用 | 失败 |
|-------|-------|-------|------|---------|----------|------|
| root  | 0     | 6     | $0.0232 | 38%  | 8        | 1    |
| a1b2  | 1     | 4     | $0.0062 | 22%  | 5        | 0    |
| c3d4  | 2     | 2     | $0.0018 |  9%  | 1        | 0    |
| **合计** | - | **12** | **$0.0312** | - | **14** | **1** |

Token 明细：input(新)=6,120 / cache_hit=12,300(67%) / output=1,204

### 8.2 工具效率分析（全树）
- 各工具调用次数、失败次数（跨所有 agent 汇总）
- 重复读取检测（同一文件在同一 agent 被 Read >1 次 → 建议合并）
- 无效调用检测（Bash exit≠0 后未出现修复行为）
- Sub-agent 委派效率：委派的工作量是否与 sub-agent 的成本匹配

### 8.3 上下文压力（每 agent 独立）
每个 agent 的 ctx 压力时序（按 Stop/轮次更新），ASCII 折线，识别哪个 agent 上下文积累过快。

### 8.4 质量观察（LLM 可选）
若启用 `--judge`，调用轻量模型对整个会话（含 sub-agent 输出）进行：
- 目标达成度评估
- 任务分解合理性（sub-agent 的委派是否粒度合适）
- 潜在可优化点（提炼为 skill 建议）

---

## 9. SKILL.md 触发条件

```yaml
name: auditit
triggers:
  - "audit"
  - "monitor agent"
  - "watch claude session"
  - "审计 agent"
  - "监控 claude"
  - "看看 agent 在做什么"
  - "开启审计"
```

调用后 Claude 执行：
1. 确认当前在 tmux 中（否则报错提示）
2. 运行 `workbench.sh start`，打开 audit pane
3. 将 settings.json 路径告知用户
4. 等待用户在 worker pane 启动 claude

---

## 10. 实现阶段

| 阶段 | 内容 | 完成标志 |
|------|------|----------|
| P0 | `audit_hook.sh` + `gen_settings.py` + `util.py` | Hook 正确捕获全事件写入 JSONL |
| P1 | `display.py` 基础渲染（事件流 + 工具统计） | Audit pane 能实时展示工具调用 |
| P2 | `display.py` 状态栏（成本 + ctx 压力 + 轮次） | 顶部状态栏实时更新 |
| P3 | `workbench.sh`（start/stop/replay） | 能一键启动 audit pane |
| P4 | `analyzer.py` + `report.md` 生成 | 会话结束后有分析报告 |
| P5 | `SKILL.md` + Mode B（launch）| 完整 skill 可用 |
