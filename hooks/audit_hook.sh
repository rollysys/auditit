#!/usr/bin/env bash
# auditit hook script.
# Called by Claude Code for every registered hook event.
# Usage: audit_hook.sh <EventName>
# Stdin: full event JSON from Claude Code.
#
# Log path is read from $AUDITIT_DIR/current.state (written by workbench.sh).
# This avoids embedding quoted paths in the hook command string.
#
# Performance note: this script runs for EVERY tool call — keep it minimal.

EVENT="$1"

AUDITIT_DIR="${AUDITIT_DIR:-/tmp/auditit}"
STATE_FILE="$AUDITIT_DIR/current.state"

[ -f "$STATE_FILE" ] || exit 0

# Only log from the designated target process.
# workbench.sh sets AUDITIT_TARGET=1 on the audited claude; all other
# claude sessions (including the auditor itself) must not write to the log.
[ "${AUDITIT_TARGET:-0}" = "1" ] || exit 0

# shellcheck disable=SC1090
source "$STATE_FILE"

LOG="${SESSION_DIR}/audit.jsonl"
[ -n "$SESSION_DIR" ] || exit 0

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Compact to single line: Claude Code may send pretty-printed (multi-line) JSON.
# Removing literal newlines keeps each JSONL record on one line.
INPUT=$(tr -d '\n\r')

# Quick sanity check: must start with '{' to be a JSON object
case "$INPUT" in
    "{"*) ;;
    *) exit 0 ;;
esac

# Build the full line in a variable, then write in one shot to minimize
# interleaving when multiple hooks fire concurrently.
LINE=$(printf '{"ts":"%s","event":"%s","data":%s}' "$TS" "$EVENT" "$INPUT")
printf '%s\n' "$LINE" >> "$LOG"
exit 0
