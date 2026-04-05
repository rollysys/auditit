---
name: auditit
description: |
  Passive real-time monitor for Claude Code sessions. Generates per-session settings
  with audit hooks and uses AUDITIT_TARGET env var for process isolation.
  Opens a tmux audit pane showing tool calls, token usage, context pressure, cost, and
  full sub-agent tree. Use when user wants to: audit or monitor a Claude session, observe
  agent tool calls and cost, check context pressure, watch sub-agent activity.
  Triggers on: "audit session", "monitor claude", "start audit", "auditit start/stop/status/replay/report".
---

# auditit: Claude Code Session Audit Monitor

Real-time audit monitor for any Claude Code session. Opens a tmux pane that shows
tool calls, assistant messages, token usage, context pressure, and cost — including
full sub-agent tree visibility.

## What it does

- Generates a **per-session** `settings.json` with audit hooks (global `~/.claude/settings.json` is never modified)
- Uses `AUDITIT_TARGET=1` env var to isolate the audited process — only that process writes to the audit log
- Opens an audit pane in tmux showing a live, nested tree of all agent activity
- Sub-agents inherit `AUDITIT_TARGET` from the parent process, so they are automatically covered
- Generates a Markdown analysis report after the session

## Prerequisites

- `tmux` 3.0+
- `python3` with `rich` (`pip install rich`)
- `claude` CLI (logged in)

## Directory structure

```
auditit/
├── SKILL.md
├── design.md
├── hooks/
│   └── audit_hook.sh          # Hook script (AUDITIT_TARGET gated → JSONL)
└── scripts/
    ├── workbench.sh            # Entry point: tmux orchestration
    ├── display.py              # Live display engine (sub-agent tree, cost, ctx)
    ├── gen_settings.py         # Generate per-session settings.json with audit hooks
    ├── analyzer.py             # Post-session report generator
    └── util.py                 # Shared utilities
```

## Quick start

```bash
SKILL_DIR="$HOME/.claude/skills/auditit/scripts"

# 1. Start audit pane + worker (inside tmux)
bash "$SKILL_DIR/workbench.sh" start --model sonnet

# 2. The worker pane launches claude with AUDITIT_TARGET=1
#    Only that process writes to audit.jsonl — your own session is not captured.

# 3. When done, stop and close panes
bash "$SKILL_DIR/workbench.sh" stop

# 4. Generate analysis report (optional)
bash "$SKILL_DIR/workbench.sh" report
```

### Manual mode (no --model)

```bash
bash "$SKILL_DIR/workbench.sh" start
# workbench prints the exact command, e.g.:
#   AUDITIT_TARGET=1 claude --settings /tmp/auditit/session-xxx/settings.json
# Run that in another pane.
```

## Commands

| Command | Description |
|---------|-------------|
| `start [--model MODEL] [--repo-root DIR]` | Generate session settings + open audit pane (with --model: also opens worker pane) |
| `launch --prompt STR [--model MODEL]` | Run claude -p in background with audit pane |
| `stop` | Close audit/worker panes |
| `replay [audit.jsonl]` | Replay an existing audit log |
| `status` | Show current session status |
| `report [--session-dir DIR]` | Generate Markdown report from session |

## How Claude should use this skill

### When invoked with a prompt (e.g. `/auditit 分析潍柴动力`)

Use `launch` — it runs the task headlessly and shows the audit pane:

```bash
bash "$SKILL_DIR/workbench.sh" launch --prompt '用户给出的 prompt'
```

Optional flags: `--model sonnet|opus`, `--max-turns N`, `--repo-root DIR`

### When the user wants to monitor their own interactive `claude` session

Use `start` — generates a session-specific settings.json with audit hooks and opens the audit pane.

1. Confirm they are inside a tmux session
2. Run `workbench.sh start` (or `start --model sonnet` for a managed worker)
3. Without `--model`: tell the user the exact command:
   `AUDITIT_TARGET=1 claude --settings <session>/settings.json`
4. After they finish, run `workbench.sh stop`

### Cleanup and reporting

```bash
bash "$SKILL_DIR/workbench.sh" stop     # close audit/worker panes
bash "$SKILL_DIR/workbench.sh" report   # generate Markdown analysis
```

## Process isolation

auditit uses **two mechanisms** to ensure only the audited process writes to the log:

1. **Per-session settings.json**: `gen_settings.py` merges the user's global settings with
   audit hooks into a session-local file. Only the audited claude loads this file via `--settings`.

2. **`AUDITIT_TARGET=1` env var**: `audit_hook.sh` checks this variable and exits immediately
   if it is not set. This prevents other claude sessions (including the auditor itself) from
   writing to the audit log, even if they happen to have the hooks loaded.

Sub-agents spawned via the Agent tool inherit both the settings and the env var from their
parent process, so they are automatically captured. Events are linked via `agent_id` and
rendered with indentation showing nesting depth:

```
15:32:48  ┌ 🤖 SUBAGENT  depth=1  id=a1b2c3d4
15:32:49  │  📖 Read  src/main.cpp  →  ✔  312 lines
15:32:51  │  ┌ 🤖 SUBAGENT  depth=2  id=e5f6g7h8
15:32:52  │  │  🔍 Grep  "TODO"  →  ✔  5 matches
15:32:53  │  └ 🤖 SUBAGENT STOP  ✔  turns=2  $0.0018
15:32:55  └ 🤖 SUBAGENT STOP  ✔  turns=4  $0.0062
```

## Display filtering

`display.py` supports `--session-id SID` to filter events by session (prefix match).
Useful when replaying an audit.jsonl that contains mixed sessions:

```bash
python3 display.py --replay audit.jsonl --session-id 1fa96f8c
```

## Session data

Sessions are stored under `/tmp/auditit/` (override with `AUDITIT_DIR`):

```
/tmp/auditit/
├── current.state              # Active session state
└── session-20260403-152300/
    ├── audit.jsonl            # All hook events (append-only JSONL)
    ├── settings.json          # Per-session settings with audit hooks
    └── report.md              # Post-session analysis report
```
