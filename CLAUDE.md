# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

auditit is a local session audit viewer for Claude Code. It reads Claude Code's native transcript files (`~/.claude/projects/<encoded>/<sid>.jsonl`) and presents them in a Web UI with cost analysis, session timeline, memory/skills browsing, and cross-session dashboard stats. **No hooks are installed** — the system is purely read-only.

## Architecture

Pure Python stdlib, zero external dependencies:

- **server.py** — Threaded HTTP server on `:8765`. Discovers sessions by scanning `~/.claude/projects/` for transcript JSONL files. Parses transcripts with streaming merge (assistant messages are streamed as N lines per message.id; parser buffers and merges content arrays, taking the last line's usage as cumulative). Computes cost at serve-time from `PRICING`/`CTX_WINDOW` tables. Window-scoped cost/tokens for dashboard time ranges via `_compute_window_usage()`. Cost cache at `~/.claude-audit/_cost_cache/`.
- **web/index.html** — Single-file Web UI (Sessions | Dashboard | Memory | Skills tabs). Dark theme. Chart.js + marked.js inlined for offline use. Client-side session grouping by last_active_at, scripted/interactive toggle, paginated tables.
- **install.py** — Hook installer (currently unused — hooks uninstalled). Copies hook to `~/.claude/hooks/auditit/` and writes to `~/.claude/settings.json`. Kept for potential future use.
- **hook.py** — Hook script (currently not installed). Was used for event capture before the transcript-viewer architecture.
- **migrate_flatten.py** — One-shot migration tools: flatten old `YYYY-MM-DD/<sid>/` layout to flat `<sid>/`, dedupe, and backfill scripted-session classification.
- **tests/test_transcript.py** — 12 unit tests for the transcript parser (streaming merge, synthetic messages, system events, path safety, etc.).
- **tests/smoke.py** — Hook-era smoke tests (retained for reference).

## Key Design Constraints

- **No hooks installed**: Zero runtime overhead. All data comes from Claude Code's own transcript files. Never modify `~/.claude/projects/` — read only.
- **Transcript streaming merge**: A single assistant message produces N JSONL lines (one per content block: thinking, text, tool_use), all sharing the same `message.id`. The parser MUST merge by id, not skip duplicates. The LAST line's usage is cumulative; earlier lines have partial values.
- **`<synthetic>` messages**: Model `<synthetic>` means Claude Code's client-side response (e.g. "Not logged in"). Must be rendered (not skipped) but excluded from usage accumulation.
- **`isMeta` lines**: Skip for display — they are internal/command messages.
- **Scripted detection**: Only `entrypoint=sdk-cli` is reliable. `permissionMode=bypassPermissions` has false positives (interactive users with `--dangerously-skip-permissions`).
- **Window-scoped cost**: Dashboard time ranges (1d/7d/30d) must compute cost only for turns within the window, not lifetime. `_compute_window_usage()` re-parses the transcript for each session.
- **Cost cache** (`~/.claude-audit/_cost_cache/<sid>.json`): Keyed by file size. Delete the cache dir to force re-computation.
- **Pricing/context tables need manual updates**: New models require updating `PRICING` and `CTX_WINDOW` dicts in server.py.

## Common Commands

```bash
# Run web server
python3 server.py                  # starts on http://0.0.0.0:8765

# Run tests
python3 tests/test_transcript.py   # 12 transcript parser tests
python3 tests/smoke.py             # hook-era smoke tests

# Clear cost cache (forces re-computation on next load)
rm -rf ~/.claude-audit/_cost_cache/

# Legacy migration (from hook-era data)
python3 migrate_flatten.py                  # flatten date-partitioned dirs
python3 migrate_flatten.py --dedupe-flat    # remove duplicate events
python3 migrate_flatten.py --backfill-mode  # classify scripted sessions
```

## Data Model

**Primary data source**: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`

Transcript line types the parser handles:

| Type | Key Fields | Parser Action |
|------|-----------|---------------|
| `user` | message.content (str or [{tool_result}]) | user_text / tool_result events |
| `assistant` | message.{id, model, content[], usage} | Streaming merge → assistant_text / thinking / tool_use |
| `system` | subtype, content | api_error / compact_boundary → system_event |
| `queue-operation` | operation, content | Extract first prompt from enqueue |
| `attachment` | attachment, message=None | Skip (but don't crash on null message) |
| `file-history-snapshot` | snapshot | Skip |
| `permission-mode` | permissionMode | Extract permission mode |

**Legacy data**: `~/.claude-audit/<session-id>/` (from hook era, read-only fallback)

## API Endpoints

- `GET /api/sessions` — list all sessions (transcript scan, cached 30s)
- `GET /api/sessions/<sid>/transcript` — parsed transcript events (streaming-merged)
- `GET /api/sessions/<sid>/events` — legacy audit events (fallback)
- `GET /api/sessions/<sid>/meta` — metadata + summary (transcript fallback when no audit dir)
- `GET /api/stats?range=7d&exclude_scripted=1` — dashboard aggregates (window-scoped)
- `GET /api/memory` / `GET /api/skills` — memory + skills viewer
- `DELETE /api/sessions/<sid>` — delete session (cascade sub-agents)
