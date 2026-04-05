# auditit

Real-time, passive audit monitor for Claude Code sessions. Opens a 3-pane tmux layout (auditor + live audit display + worker) that captures every tool call, assistant message, token usage, context pressure, and cost — including full sub-agent tree visibility — without modifying your global Claude settings.

```
┌──────────────────────┬──────────────────────────┐
│                      │  AUDIT (live tree view)  │
│  AUDITOR             │  14:32:48  📖 Read ...   │
│  (your claude, not   │  14:32:51  ┌ 🤖 SUBAGENT │
│   audited, for log   │  14:32:52  │  🔍 Grep ...│
│   review)            │──────────────────────────│
│                      │  WORKER (sonnet)         │
│                      │  AUDITIT_TARGET=1        │
│                      │  claude ...              │
└──────────────────────┴──────────────────────────┘
```

## Why

Claude Code exposes hooks that fire on every tool call, but injecting hooks globally means every Claude process you run gets captured — including the one you use to *review* the audit log. `auditit` uses two layers of isolation so only the session you want to audit writes to the log:

1. **Per-session settings**: the audit hooks live in a session-local `settings.json`, passed to the audited process via `claude --settings <path>`. Your global `~/.claude/settings.json` is never touched.
2. **`AUDITIT_TARGET=1` env var**: the hook script exits immediately unless this variable is set. Even if the hooks somehow load in another Claude process, they stay silent.

Sub-agents spawned via the Agent tool inherit both the settings file and the env var from their parent, so nested agents are captured automatically.

## Prerequisites

- `tmux` 3.0+
- `python3` with `rich` (`pip install rich`)
- `claude` CLI (logged in)

## Install

As a Claude Code skill:

```bash
git clone https://github.com/rollysys/auditit.git ~/.claude/skills/auditit
```

Or just clone anywhere and call `workbench.sh` directly.

## Quick start

```bash
SKILL_DIR="$HOME/.claude/skills/auditit/scripts"

# Start audit session with a managed worker claude (creates new tmux window/session)
bash "$SKILL_DIR/workbench.sh" start --model sonnet --repo-root /path/to/project

# When done:
bash "$SKILL_DIR/workbench.sh" stop

# Generate Markdown analysis report
bash "$SKILL_DIR/workbench.sh" report
```

### Behaviour

- **Outside tmux**: creates a new tmux session `audit-<timestamp>` with 3 panes and attaches.
- **Inside tmux**: creates a new window in the current session with 3 panes and switches focus.
- **`--model` omitted**: only creates the auditor + audit display panes, prints the exact command to run the audited `claude` yourself:
  ```
  AUDITIT_TARGET=1 claude --settings /tmp/auditit/session-xxx/settings.json
  ```

## Commands

| Command | Description |
|---------|-------------|
| `start [--model MODEL] [--repo-root DIR]` | Open 3-pane audit layout (with `--model`: also starts a managed worker) |
| `launch --prompt STR [--model MODEL] [--max-turns N]` | Run `claude -p` headlessly with live audit pane |
| `stop` | Kill the audit display and worker panes (auditor pane preserved) |
| `replay [audit.jsonl]` | Replay a past audit log offline |
| `status` | Show current session state |
| `report [--session-dir DIR]` | Generate post-session Markdown report |

## Display filtering

`display.py` supports `--session-id SID` to filter events by session (prefix match). Useful when replaying an `audit.jsonl` that accidentally captured mixed sessions:

```bash
python3 scripts/display.py --replay audit.jsonl --session-id 1fa96f8c
```

## Session data

Sessions are stored under `/tmp/auditit/` (override with `AUDITIT_DIR`):

```
/tmp/auditit/
├── current.state              # active session state (session dir + pane IDs)
└── session-20260405-143652/
    ├── audit.jsonl            # all hook events (append-only JSONL)
    ├── settings.json          # per-session settings with audit hooks
    └── report.md              # post-session analysis report
```

## Architecture

```
auditit/
├── SKILL.md                   # Claude Code skill manifest
├── design.md                  # Design notes
├── hooks/
│   └── audit_hook.sh          # Gated by AUDITIT_TARGET, writes JSONL
└── scripts/
    ├── workbench.sh           # tmux orchestration (start/stop/launch/report)
    ├── display.py             # Live tree renderer with sub-agent nesting
    ├── gen_settings.py        # Generate per-session settings.json
    ├── analyzer.py            # Post-session report generator
    └── util.py                # Shared utilities
```

## License

MIT. See [LICENSE](LICENSE).
