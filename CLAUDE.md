# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

auditit is a global audit system for Claude Code sessions. It passively captures every session's prompts, tool calls, sub-agents, tokens, costs, context pressure, and process runtime info via Claude Code's hook mechanism, writing to `~/.claude-audit/<session_id>/`. A local web UI (`server.py :8765`) provides real-time tail, history replay, memory/skills viewer, and cross-session dashboard stats.

## Architecture

Pure Python stdlib, zero external dependencies:

- **hook.py** — Python script registered as a global Claude Code hook for 25 events. On each hook fire: snapshots claude process runtime info from `/proc`, appends JSONL with optional `"proc"` sidecar. On `SessionStart`: double-forks a watchdog daemon that uses `os.pidfd_open` + `poll()` to detect SIGKILL/OOM (writes `death.json` if claude dies without SessionEnd). On `SessionEnd`: parses `transcript_path` for usage/model/cost/ctx_peak, writes `summary.json`, performs sub-agent slicing from `SubagentStop` transcripts, then atomically gzip-compresses the JSONL with line-count verification.
- **server.py** — Threaded HTTP server on `:8765`. REST API for sessions/memory/skills/stats + SSE live tail. Computes cost and context pressure at serve-time from `PRICING`/`CTX_WINDOW` tables (no backfill needed). Provider detection via `env.json` (captured at hook time) with model-name fallback. Flat layout (no date partition).
- **install.py** — Installer that copies hook.py to `~/.claude/hooks/auditit/` and writes hook entries into `~/.claude/settings.json`. Uses `# auditit` marker for idempotent install/uninstall (path-independent). Atomic write via tempfile+rename, fcntl lock, timestamped backups.
- **web/index.html** — Single-file Web UI (Sessions | Memory | Skills | Dashboard tabs). Dark theme, SSE-based live tail, JSON syntax highlighting for tool input/output, markdown rendering, death record display.
- **migrate_flatten.py** — One-shot migration: `~/.claude-audit/YYYY-MM-DD/<sid>/` → `~/.claude-audit/<sid>/` (flat). Also `--dedupe-flat` and `--backfill-mode`.

## Key Design Constraints

- **hook.py must never block or crash**: It runs as a global hook; a broken hook blocks all Claude sessions. Fix by running `python3 install.py uninstall` from a regular terminal, fix the bug, then reinstall.
- **`~/.claude-audit/` files are immutable audit evidence**: Never modify, only read. Deletion must go through the Web UI or `DELETE /api/sessions/...`.
- **Pricing/context tables need manual updates**: New models require updating both `server.py` (`PRICING`, `CTX_WINDOW`) and `docs/claude-pricing.md` / `docs/claude-context-windows.md`.
- **Watchdog uses `os.pidfd_open` (Python 3.9+, Linux 5.3+)**: Falls back to polling `os.kill(pid, 0)` on macOS/older kernels. The watchdog is spawned via double-fork in SessionStart and writes `death.json` when the claude process disappears.
- **Proc snapshots read only `/proc/pid/status` + `stat`**: Avoid `smaps_rollup` (~6ms for large processes); RSS from `status` is sufficient.

## Common Commands

```bash
# Install/uninstall hooks
python3 install.py doctor          # pre-flight checks
python3 install.py install         # register hooks + copy hook.py
python3 install.py install --dry-run  # preview without writing
python3 install.py uninstall       # remove hooks (marker-based, path-independent)
python3 install.py status          # show which events have hooks

# Run web server
python3 server.py                  # starts on http://0.0.0.0:8765

# Migrate old date-partitioned data
python3 migrate_flatten.py                  # flatten + consolidate
python3 migrate_flatten.py --dedupe-flat    # remove duplicate events
python3 migrate_flatten.py --backfill-mode  # classify scripted sessions

# Read audit logs (for debugging)
zcat ~/.claude-audit/<session-id>/audit.jsonl.gz  # compressed
cat ~/.claude-audit/<session-id>/audit.jsonl       # active
```

## Data Model

Session directories live under `~/.claude-audit/<session_id>/` (flat, no date partition):

| File | Written by | Contents |
|------|-----------|----------|
| `audit.jsonl` | hook.py | `{"ts":"...","event":"...","data":{...},"proc":{...}}` per line |
| `audit.jsonl.gz` | hook.py (SessionEnd) | Atomic gzip of jsonl, verified line count |
| `summary.json` | hook.py (SessionEnd) | model, num_turns, duration_ms, usage, ctx_peak_tokens |
| `metadata.json` | server.py (first read) | Cached prompt, model, cwd, started_at, last_active_at |
| `env.json` | hook.py (first event) | anthropic_base_url, use_bedrock, use_vertex, claude_pid, is_headless, parent_cmd |
| `meta.json` | hook.py (sub-agent only) | is_subagent, parent_session_id, agent_type, description |
| `death.json` | watchdog (on kill) | ts, claude_pid, event="process_death_detected", note |
| `.watchdog` | hook.py (SessionStart) | Flag file containing claude PID; removed by SessionEnd |

Sub-agent directories: `<parent_sid>__agent__<agent_id>/` (siblings, not nested). `__agent__` is the separator constant.

## API Endpoints (server.py)

- `GET /api/sessions` — list all sessions with has_death flag
- `GET /api/sessions/<sid>/events` — all events (handles .gz transparently)
- `GET /api/sessions/<sid>/stream` — SSE live tail
- `GET /api/sessions/<sid>/meta` — metadata + summary + death record (cost computed on-the-fly)
- `GET /api/memory` — memory file index across all projects
- `GET /api/memory/file?path=<abs>` — read a single memory file (strict allow-list)
- `GET /api/stats` — cross-session dashboard aggregates
- `GET /api/skills` — list user-level skills
- `GET /api/skills/file?name=<n>&path=<rel>` — read a skill file
- `DELETE /api/sessions/<sid>` — delete session (cascade sub-agents, active check)
