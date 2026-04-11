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
