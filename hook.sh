#!/usr/bin/env bash
# hook.sh — Global audit hook for Claude Code.
# Called by Claude Code for every registered hook event.
# Usage: hook.sh <EventName>
# Stdin: full event JSON from Claude Code.
#
# Writes to ~/.claude-audit/YYYY-MM-DD/<sessionId>/audit.jsonl
# On SessionEnd: compresses to .jsonl.gz via an atomic tmp+rename path
# with line-count verification before deleting the original.

EVENT="$1"
AUDIT_DIR="${HOME}/.claude-audit"

# Read stdin, compact to single line
INPUT=$(tr -d '\n\r')
case "$INPUT" in "{"*) ;; *) exit 0 ;; esac

# Extract sessionId via grep (handles optional spaces around colon)
SESSION_ID=$(echo "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"\([^"]*\)".*/\1/')
[ -n "$SESSION_ID" ] || exit 0

DATE_DIR=$(date -u +"%Y-%m-%d")
SESSION_DIR="${AUDIT_DIR}/${DATE_DIR}/${SESSION_ID}"
JSONL="${SESSION_DIR}/audit.jsonl"

[ -d "$SESSION_DIR" ] || mkdir -p "$SESSION_DIR"

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
printf '%s\n' "{\"ts\":\"${TS}\",\"event\":\"${EVENT}\",\"data\":${INPUT}}" >> "$JSONL"

# SessionEnd: parse transcript for real usage/model/turns/duration, write
# summary.json, then compress jsonl atomically (tmpfile + line-count verify).
#
# Note: Claude Code's SessionEnd hook data itself only carries
# {session_id, transcript_path, cwd, reason, hook_event_name}. Cost, token
# counts, turn count, and duration live in the file at transcript_path —
# one JSON object per line, with type=user and type=assistant entries.
# The last assistant entry's message.usage carries the cumulative usage.
if [ "$EVENT" = "SessionEnd" ]; then
    export SESSION_DIR
    python3 -c '
import sys, json, os, gzip
from datetime import datetime

d = json.load(sys.stdin)
transcript_path = d.get("transcript_path", "")

def _parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None

model = ""
raw_usage = {}
num_turns = 0
first_ts = None
last_ts = None

if transcript_path and os.path.exists(transcript_path):
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as tf:
            for line in tf:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                t = obj.get("type", "")
                if t == "user":
                    # Real user turns — attachment rows have a different type
                    # and are excluded automatically.
                    num_turns += 1
                elif t == "assistant":
                    msg = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
                    if msg.get("model"):
                        model = msg["model"]
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        raw_usage = u
    except OSError:
        pass

duration_ms = 0
if first_ts and last_ts:
    duration_ms = int((last_ts - first_ts).total_seconds() * 1000)

# Extract just the token fields we price on. cache_creation has an
# ephemeral_5m_input_tokens / ephemeral_1h_input_tokens split — we record
# both so server.py can price cache writes exactly.
cc = raw_usage.get("cache_creation") or {}
if not isinstance(cc, dict):
    cc = {}
usage = {
    "input_tokens":                raw_usage.get("input_tokens", 0) or 0,
    "output_tokens":               raw_usage.get("output_tokens", 0) or 0,
    "cache_read_input_tokens":     raw_usage.get("cache_read_input_tokens", 0) or 0,
    "cache_creation_input_tokens": raw_usage.get("cache_creation_input_tokens", 0) or 0,
    "cache_creation_5m_tokens":    cc.get("ephemeral_5m_input_tokens", 0) or 0,
    "cache_creation_1h_tokens":    cc.get("ephemeral_1h_input_tokens", 0) or 0,
}

summary = {
    "reason":      d.get("reason", ""),
    "model":       model,
    "num_turns":   num_turns,
    "duration_ms": duration_ms,
    "usage":       usage,
}

sd = os.environ.get("SESSION_DIR", "")
if not sd:
    sys.exit(0)

with open(os.path.join(sd, "summary.json"), "w") as f:
    json.dump(summary, f, indent=2)

jsonl = os.path.join(sd, "audit.jsonl")
gz = jsonl + ".gz"
gz_tmp = gz + ".tmp"

if not (os.path.exists(jsonl) and os.path.getsize(jsonl) > 0):
    sys.exit(0)

with open(jsonl, "rb") as fin:
    src_bytes = fin.read()
src_lines = src_bytes.count(b"\n")

# ===== Sub-agent slicing =====
# Walk the parent audit.jsonl; every SubagentStart opens a new sub-agent
# directory at <date>/<immediate_parent_name>__agent__<agent_id>/. Every
# event between a matching Start/Stop is appended to the new layer AND to
# every ancestor layer still open on the stack (so a parent sub-agent sees
# a complete view of its own nested descendants). On Stop, the layer is
# finalized: meta.json + summary.json + atomic gzip + delete plain jsonl.
# Any layers left unclosed at EOF (crashed sub-agent) are still finalized
# with reason="unclosed".
#
# The parent audit.jsonl itself is never touched by this step — it stays
# the source of truth and is compressed normally afterwards.
parent_dir_name = os.path.basename(sd.rstrip("/"))
date_dir_path   = os.path.dirname(sd.rstrip("/"))
slicing_stack = []

def _open_sub_layer(agent_id, agent_type, desc, start_ts, immediate_parent_name):
    name = immediate_parent_name + "__agent__" + (agent_id or "unknown")
    dpath = os.path.join(date_dir_path, name)
    os.makedirs(dpath, exist_ok=True)
    fh = open(os.path.join(dpath, "audit.jsonl"), "ab")
    return {
        "dir":         dpath,
        "name":        name,
        "fh":          fh,
        "agent_id":    agent_id,
        "agent_type":  agent_type,
        "desc":        desc or "",
        "start_ts":    start_ts,
        "immediate_parent_name": immediate_parent_name,
        "tool_count":  0,
        "event_count": 0,
        "first_ts":    None,
        "last_ts":     None,
    }

def _write_to(entry, raw_line, ts):
    entry["fh"].write(raw_line)
    entry["event_count"] += 1
    if entry["first_ts"] is None:
        entry["first_ts"] = ts
    entry["last_ts"] = ts

def _close_sub_layer(entry, reason):
    try:
        entry["fh"].flush()
        entry["fh"].close()
    except Exception:
        pass

    # meta.json — immutable descriptor
    meta_obj = {
        "is_subagent":        True,
        "parent_session_id":  entry["immediate_parent_name"],
        "root_session_id":    parent_dir_name,
        "agent_id":           entry["agent_id"],
        "agent_type":         entry["agent_type"],
        "description":        (entry["desc"] or "")[:500],
        "start_ts":           entry["start_ts"],
    }
    try:
        with open(os.path.join(entry["dir"], "meta.json"), "w") as f:
            json.dump(meta_obj, f, indent=2)
    except OSError:
        pass

    # summary.json — matches the parent summary shape enough for list_sessions
    # to show duration and tool count; usage is intentionally absent so
    # server.compute_cost returns 0 and the UI renders cost as "—".
    duration_ms_sub = 0
    t0 = _parse_ts(entry["first_ts"])
    t1 = _parse_ts(entry["last_ts"])
    if t0 and t1:
        duration_ms_sub = int((t1 - t0).total_seconds() * 1000)
    sub_summary = {
        "is_subagent":       True,
        "reason":            reason,
        "parent_session_id": entry["immediate_parent_name"],
        "agent_type":        entry["agent_type"],
        "agent_id":          entry["agent_id"],
        "description":       (entry["desc"] or "")[:500],
        "num_tool_calls":    entry["tool_count"],
        "num_turns":         0,
        "duration_ms":       duration_ms_sub,
    }
    try:
        with open(os.path.join(entry["dir"], "summary.json"), "w") as f:
            json.dump(sub_summary, f, indent=2)
    except OSError:
        pass

    # Compress the sub-agent jsonl atomically (same pattern as parent).
    sub_jsonl  = os.path.join(entry["dir"], "audit.jsonl")
    sub_gz     = sub_jsonl + ".gz"
    sub_gz_tmp = sub_gz + ".tmp"
    if not (os.path.exists(sub_jsonl) and os.path.getsize(sub_jsonl) > 0):
        return
    try:
        with open(sub_jsonl, "rb") as fin:
            sub_bytes = fin.read()
        sub_src_lines = sub_bytes.count(b"\n")
        with gzip.open(sub_gz_tmp, "wb", compresslevel=6) as fout:
            fout.write(sub_bytes)
        with gzip.open(sub_gz_tmp, "rt") as f:
            sub_gz_lines = sum(1 for _ in f)
    except Exception:
        try: os.remove(sub_gz_tmp)
        except OSError: pass
        return
    if sub_gz_lines != sub_src_lines:
        try: os.remove(sub_gz_tmp)
        except OSError: pass
        return
    try:
        os.replace(sub_gz_tmp, sub_gz)
        os.remove(sub_jsonl)
    except OSError:
        pass

# Claude Code never populates SubagentStart.data.description — the
# human-readable task comes from the Task/Agent tool call that preceded
# the Start (its tool_input has description + prompt + subagent_type).
# Track the most recent such PreToolUse so we can stamp the sub-agent
# layer with it. Consumed + cleared on the next SubagentStart.
last_task_desc = ""

for raw_line in src_bytes.splitlines(keepends=True):
    stripped = raw_line.strip()
    if not stripped:
        continue
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        continue
    ev  = obj.get("event", "")
    dat = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
    ts  = obj.get("ts", "")

    if ev == "PreToolUse" and dat.get("tool_name") in ("Task", "Agent"):
        ti = dat.get("tool_input", {}) if isinstance(dat.get("tool_input"), dict) else {}
        last_task_desc = (ti.get("description") or ti.get("prompt") or "")[:500]
        # Fall through so the event itself still gets mirrored into any
        # currently-open sub-agent layers below.

    if ev == "SubagentStart":
        immediate_parent_name = slicing_stack[-1]["name"] if slicing_stack else parent_dir_name
        agent_id   = dat.get("agent_id", "") or ""
        agent_type = dat.get("agent_type", "") or ""
        # Prefer the last Task/Agent PreToolUse description; fall back
        # to any description that may somehow be on the event itself.
        desc = last_task_desc or dat.get("description") or dat.get("prompt") or ""
        last_task_desc = ""  # consume — next Task call must refresh it
        try:
            layer = _open_sub_layer(agent_id, agent_type, desc, ts, immediate_parent_name)
        except OSError:
            continue
        # Start event is visible both in the new layer AND in every ancestor.
        for anc in slicing_stack:
            _write_to(anc, raw_line, ts)
        _write_to(layer, raw_line, ts)
        slicing_stack.append(layer)
        continue

    if ev == "SubagentStop":
        for anc in slicing_stack:
            _write_to(anc, raw_line, ts)
            if anc is slicing_stack[-1]:
                pass
        if slicing_stack:
            leaving = slicing_stack.pop()
            _close_sub_layer(leaving, reason="normal")
        continue

    # Any other event: mirror into every active layer.
    if slicing_stack:
        for anc in slicing_stack:
            _write_to(anc, raw_line, ts)
            if ev in ("PostToolUse", "PostToolUseFailure"):
                anc["tool_count"] += 1

# Flush any still-open layers (crashed sub-agent: no matching SubagentStop).
while slicing_stack:
    leaving = slicing_stack.pop()
    _close_sub_layer(leaving, reason="unclosed")

# ===== End sub-agent slicing =====

try:
    with gzip.open(gz_tmp, "wb", compresslevel=6) as fout:
        fout.write(src_bytes)
    with gzip.open(gz_tmp, "rt") as f:
        gz_lines = sum(1 for _ in f)
except Exception:
    try: os.remove(gz_tmp)
    except OSError: pass
    sys.exit(0)

if gz_lines != src_lines:
    try: os.remove(gz_tmp)
    except OSError: pass
    sys.exit(0)

os.replace(gz_tmp, gz)
os.remove(jsonl)
' <<< "$INPUT" 2>/dev/null
fi

exit 0
