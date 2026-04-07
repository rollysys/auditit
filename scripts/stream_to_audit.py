#!/usr/bin/env python3
"""
stream_to_audit.py — Convert Claude Code stream-json output to audit.jsonl format.

Reads NDJSON from stdin (claude --bare -p --output-format stream-json --verbose),
converts to the same event format that audit_hook.sh produces, and appends to
an audit.jsonl file. This enables auditing of --bare mode sessions where hooks
are disabled.

Usage:
    claude --bare -p --output-format stream-json --verbose -- "prompt" \
        | python3 stream_to_audit.py /tmp/auditit/session-xxx/audit.jsonl

Stream-json message types mapped to audit events:
    system (init)   → SessionStart
    user            → UserPromptSubmit
    assistant       → PreToolUse / PostToolUse (for tool_use blocks)
                    → Stop (for text blocks / end of turn)
    result          → SessionEnd
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_event(fh, event: str, data: dict, base_url: str = "") -> None:
    line = json.dumps(
        {"ts": now_ts(), "event": event, "base_url": base_url, "data": data},
        ensure_ascii=False,
    )
    fh.write(line + "\n")
    fh.flush()


def process_line(raw: str, fh, state: dict) -> None:
    raw = raw.strip()
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return

    msg_type = obj.get("type", "")
    session_id = obj.get("session_id", state.get("session_id", ""))
    if session_id:
        state["session_id"] = session_id

    base_data = {"session_id": session_id}

    if msg_type == "system" and obj.get("subtype") == "init":
        model = obj.get("model", "")
        cwd = obj.get("cwd", "")
        state["model"] = model
        write_event(fh, "SessionStart", {
            **base_data,
            "hook_event_name": "SessionStart",
            "source": "startup",
            "model": model,
            "cwd": cwd,
        })

    elif msg_type == "user":
        message = obj.get("message", {})
        # Extract text from user message content
        content = message.get("content", []) if isinstance(message, dict) else []
        prompt = ""
        if isinstance(content, str):
            prompt = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    prompt += block.get("text", "")
                elif isinstance(block, str):
                    prompt += block

        # Check if this is a tool_result (not a user prompt)
        tool_result = obj.get("tool_use_result")
        if tool_result is not None:
            # This is a tool result message — emit PostToolUse for pending tools
            _emit_tool_results(obj, fh, state)
            return

        if prompt:
            write_event(fh, "UserPromptSubmit", {
                **base_data,
                "hook_event_name": "UserPromptSubmit",
                "prompt": prompt,
            })

    elif msg_type == "assistant":
        message = obj.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            return

        has_text = False
        last_text = ""

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "tool_use":
                tool_name = block.get("name", "?")
                tool_input = block.get("input", {})
                tool_use_id = block.get("id", "")
                # Emit PreToolUse
                write_event(fh, "PreToolUse", {
                    **base_data,
                    "hook_event_name": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "tool_use_id": tool_use_id,
                })
                # Track pending tool for PostToolUse matching
                state.setdefault("pending_tools", {})[tool_use_id] = {
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                }

            elif block_type == "text":
                text = block.get("text", "")
                if text.strip():
                    has_text = True
                    last_text = text

        # If this assistant message has text and no tool_use, treat as a Stop
        if has_text and not any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        ):
            state["turns"] = state.get("turns", 0) + 1
            write_event(fh, "Stop", {
                **base_data,
                "hook_event_name": "Stop",
                "last_assistant_message": last_text,
            })

    elif msg_type == "result":
        subtype = obj.get("subtype", "")
        usage = obj.get("usage", {})
        model_usage = obj.get("modelUsage", {})
        # Determine the model from modelUsage keys
        model = state.get("model", "")
        if model_usage:
            model = next(iter(model_usage), model)

        write_event(fh, "SessionEnd", {
            **base_data,
            "hook_event_name": "SessionEnd",
            "reason": subtype,
            "num_turns": obj.get("num_turns", 0),
            "total_cost_usd": obj.get("total_cost_usd", 0),
            "duration_ms": obj.get("duration_ms", 0),
            "usage": usage,
            "model": model,
        })


def _emit_tool_results(obj: dict, fh, state: dict) -> None:
    """Emit PostToolUse events from a user message containing tool_use_result."""
    session_id = state.get("session_id", "")
    base_data = {"session_id": session_id}
    message = obj.get("message", {})
    content = message.get("content", []) if isinstance(message, dict) else []
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id", "")
        result_content = block.get("content", "")
        is_error = block.get("is_error", False)

        # Look up tool name from pending
        pending = state.get("pending_tools", {}).pop(tool_use_id, {})
        tool_name = pending.get("tool_name", "?")
        tool_input = pending.get("tool_input", {})

        event = "PostToolUseFailure" if is_error else "PostToolUse"
        write_event(fh, event, {
            **base_data,
            "hook_event_name": event,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": tool_use_id,
            "tool_response": result_content if not is_error else "",
            "error": result_content if is_error else "",
        })


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: stream_to_audit.py <audit.jsonl>", file=sys.stderr)
        sys.exit(1)

    audit_path = Path(sys.argv[1])
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    state: dict = {"turns": 0, "pending_tools": {}}

    with open(audit_path, "a", encoding="utf-8") as fh:
        for line in sys.stdin:
            try:
                process_line(line, fh, state)
            except Exception as e:
                print(f"[stream_to_audit] error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
