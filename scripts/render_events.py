"""Event parsing and tree-style terminal rendering.

Two data sources feed into a unified RenderEvent model:
  1. stream-json (claude stdout)  — conversation-level events
  2. audit.jsonl (hook output)    — tool-level audit events

TreeRenderer merges tool_start + tool_end into single lines and renders
a compact tree view using rich.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterator

from rich.console import Console

from util import local_now_str, utc_to_local


@dataclass
class RenderEvent:
    ts: str = ""
    source: str = ""       # "stream" | "audit"
    kind: str = ""         # tool_start, tool_end, text, thinking, result, session, prompt, stop, rate_limit, cost
    tool: str = ""
    tool_use_id: str = ""
    summary: str = ""
    detail: dict = field(default_factory=dict)


# ── Icons & helpers ──────────────────────────────────────────────────

_TOOL_ICONS = {
    "Read": "\U0001f4d6",     # 📖
    "Write": "\u270f\ufe0f",  # ✏️
    "Edit": "\u270f\ufe0f",   # ✏️
    "Grep": "\U0001f50d",     # 🔍
    "Glob": "\U0001f50d",     # 🔍
    "Bash": "\U0001f527",     # 🔧
    "Agent": "\U0001f916",    # 🤖
    "WebSearch": "\U0001f310", # 🌐
    "WebFetch": "\U0001f310",  # 🌐
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "\U0001f6e0")  # 🛠


def _truncate(s: str, maxlen: int = 80) -> str:
    s = s.replace("\n", " ").strip()
    return s[:maxlen] + "..." if len(s) > maxlen else s


def _summarize_tool_input(tool: str, inp: dict) -> str:
    if tool == "Read":
        return inp.get("file_path", "")
    if tool == "Write":
        return inp.get("file_path", "")
    if tool == "Edit":
        return inp.get("file_path", "")
    if tool in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if tool == "Bash":
        return _truncate(inp.get("command", ""), 120)
    if tool == "Agent":
        return _truncate(inp.get("description", inp.get("prompt", "")), 120)
    for v in inp.values():
        if isinstance(v, str):
            return _truncate(v, 120)
    return ""


# ── Stream-JSON parser ───────────────────────────────────────────────

def parse_stream_line(raw: str) -> Iterator[RenderEvent]:
    raw = raw.strip()
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return

    ts = local_now_str()
    msg_type = obj.get("type", "")

    if msg_type == "assistant":
        message = obj.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "text":
                yield RenderEvent(
                    ts=ts, source="stream", kind="text",
                    summary=_truncate(block.get("text", ""), 120),
                )
            elif block.get("type") == "thinking":
                yield RenderEvent(
                    ts=ts, source="stream", kind="thinking",
                    summary=_truncate(block.get("thinking", ""), 80),
                )
            elif block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                yield RenderEvent(
                    ts=ts, source="stream", kind="tool_start",
                    tool=tool_name,
                    tool_use_id=block.get("id", ""),
                    summary=_summarize_tool_input(tool_name, tool_input),
                    detail=tool_input,
                )

    elif msg_type == "user":
        message = obj.get("message", {})
        for block in message.get("content", []):
            if block.get("type") == "tool_result":
                is_error = block.get("is_error", False)
                yield RenderEvent(
                    ts=ts, source="stream", kind="tool_end",
                    tool_use_id=block.get("tool_use_id", ""),
                    summary="\u5931\u8d25" if is_error else "\u6210\u529f",
                    detail=block,
                )

    elif msg_type == "result":
        cost_usd = obj.get("total_cost_usd", obj.get("cost_usd", 0))
        turns = obj.get("num_turns", 0)
        duration_ms = obj.get("duration_ms", 0)
        duration_s = duration_ms / 1000 if duration_ms else obj.get("duration_seconds", 0)
        usage = obj.get("usage", {})
        total_in = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
        total_out = usage.get("output_tokens", 0)
        yield RenderEvent(
            ts=ts, source="stream", kind="cost",
            summary=f"\u8d39\u7528=${cost_usd:.3f} \u8f6e\u6b21={turns} \u8017\u65f6={duration_s:.1f}s \u8f93\u5165={total_in} \u8f93\u51fa={total_out}",
            detail=obj,
        )

    elif msg_type == "content_block_start":
        cb = obj.get("content_block", {})
        if cb.get("type") == "tool_use":
            tool_name = cb.get("name", "")
            yield RenderEvent(
                ts=ts, source="stream", kind="tool_start",
                tool=tool_name,
                tool_use_id=cb.get("id", ""),
                summary="",
                detail=cb,
            )

    elif msg_type == "error":
        err = obj.get("error", {})
        yield RenderEvent(
            ts=ts, source="stream", kind="rate_limit",
            summary=_truncate(err.get("message", str(err)), 100),
        )


# ── Codex stream-JSON parser ────────────────────────────────────────

_CODEX_ITEM_TYPE_MAP = {
    "command_execution": "Bash",
    "local_shell_call": "Bash",
    "apply_patch": "Edit",
    "web.run": "WebSearch",
    "spawn_agent": "Agent",
    "send_input": "Agent",
}


def parse_codex_stream_line(raw: str) -> Iterator[RenderEvent]:
    """Parse one line of Codex CLI --json output into RenderEvent(s)."""
    raw = raw.strip()
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return

    ts = local_now_str()
    msg_type = obj.get("type", "")

    if msg_type == "item.started":
        item = obj.get("item", {})
        item_type = item.get("type", "")
        tool = _CODEX_ITEM_TYPE_MAP.get(item_type, item_type)
        cmd = item.get("command", "")
        summary = _truncate(cmd, 120) if cmd else _truncate(str(item.get("patch", "")), 120)
        yield RenderEvent(
            ts=ts, source="stream", kind="tool_start",
            tool=tool,
            tool_use_id=item.get("id", ""),
            summary=summary,
            detail=item,
        )

    elif msg_type == "item.completed":
        item = obj.get("item", {})
        item_type = item.get("type", "")
        item_id = item.get("id", "")

        if item_type == "agent_message":
            yield RenderEvent(
                ts=ts, source="stream", kind="text",
                summary=_truncate(item.get("text", ""), 120),
            )
        elif item_type in ("command_execution", "local_shell_call", "apply_patch"):
            tool = _CODEX_ITEM_TYPE_MAP.get(item_type, item_type)
            exit_code = item.get("exit_code", None)
            status = item.get("status", "")
            is_error = (exit_code is not None and exit_code != 0) or status == "failed"
            cmd = item.get("command", "")
            summary = _truncate(cmd, 120) if cmd else ""
            yield RenderEvent(
                ts=ts, source="stream", kind="tool_end",
                tool=tool,
                tool_use_id=item_id,
                summary="\u5931\u8d25" if is_error else "\u6210\u529f",
                detail=item,
            )

        elif item_type == "collab_tool_call":
            # Sub-agent lifecycle: spawn_agent / wait / close_agent
            # Codex does NOT stream sub-agent internal tool calls;
            # only the final message from wait/close is available.
            collab_tool = item.get("tool", "")
            status = item.get("status", "")
            is_error = status == "failed"

            if collab_tool == "spawn_agent":
                prompt = item.get("prompt", "")
                yield RenderEvent(
                    ts=ts, source="stream", kind="tool_start",
                    tool="Agent",
                    tool_use_id=item_id,
                    summary=_truncate(prompt, 120),
                    detail=item,
                )
            elif collab_tool in ("wait", "close_agent"):
                # Extract sub-agent's final message from agents_states
                agents_states = item.get("agents_states", {})
                sub_msg = ""
                for tid, state in agents_states.items():
                    msg = state.get("message", "")
                    if msg:
                        sub_msg = msg
                        break
                if sub_msg:
                    yield RenderEvent(
                        ts=ts, source="stream", kind="text",
                        summary="\U0001f916 " + _truncate(sub_msg, 120),
                    )
                # Close the tool_start from spawn_agent
                if collab_tool == "close_agent":
                    yield RenderEvent(
                        ts=ts, source="stream", kind="tool_end",
                        tool="Agent",
                        tool_use_id=item_id,
                        summary="\u5931\u8d25" if is_error else "\u6210\u529f",
                        detail=item,
                    )

    elif msg_type == "turn.completed":
        usage = obj.get("usage", {})
        total_in = usage.get("input_tokens", 0) + usage.get("cached_input_tokens", 0)
        total_out = usage.get("output_tokens", 0)
        yield RenderEvent(
            ts=ts, source="stream", kind="cost",
            summary=f"\u8f93\u5165={total_in} \u8f93\u51fa={total_out}",
            detail=obj,
        )

    elif msg_type == "turn.failed":
        error = obj.get("error", {})
        yield RenderEvent(
            ts=ts, source="stream", kind="rate_limit",
            summary=_truncate(str(error), 100),
        )


# ── Audit JSONL parser ───────────────────────────────────────────────

def parse_audit_line(raw: str) -> Iterator[RenderEvent]:
    raw = raw.strip()
    if not raw:
        return
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return

    ts = utc_to_local(obj.get("ts", "")) or local_now_str()
    event_name = obj.get("event", "")
    data = obj.get("data", {})

    if event_name == "PreToolUse":
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        yield RenderEvent(
            ts=ts, source="audit", kind="tool_start",
            tool=tool_name,
            tool_use_id=data.get("tool_use_id", ""),
            summary=_summarize_tool_input(tool_name, tool_input),
            detail=data,
        )

    elif event_name in ("PostToolUse", "PostToolUseFailure"):
        is_error = event_name == "PostToolUseFailure"
        yield RenderEvent(
            ts=ts, source="audit", kind="tool_end",
            tool=data.get("tool_name", ""),
            tool_use_id=data.get("tool_use_id", ""),
            summary="\u5931\u8d25" if is_error else "\u6210\u529f",
            detail=data,
        )

    elif event_name == "SessionStart":
        yield RenderEvent(ts=ts, source="audit", kind="session", summary="\u4f1a\u8bdd\u5f00\u59cb", detail=data)

    elif event_name == "SessionEnd":
        yield RenderEvent(ts=ts, source="audit", kind="session", summary="\u4f1a\u8bdd\u7ed3\u675f", detail=data)

    elif event_name == "UserPromptSubmit":
        yield RenderEvent(ts=ts, source="audit", kind="prompt", summary=_truncate(data.get("prompt", ""), 100), detail=data)

    elif event_name == "Stop":
        yield RenderEvent(ts=ts, source="audit", kind="stop", summary=_truncate(data.get("last_assistant_message", ""), 100), detail=data)

    elif event_name in ("SubagentStart", "SubagentStop"):
        yield RenderEvent(ts=ts, source="audit", kind="session",
                          summary=f"\u5b50agent{'\u542f\u52a8' if event_name == 'SubagentStart' else '\u505c\u6b62'}",
                          detail=data)

    elif event_name in ("Notification", "TaskCompleted"):
        yield RenderEvent(ts=ts, source="audit", kind="session",
                          summary=f"{event_name}: {_truncate(str(data), 80)}", detail=data)


# ── Tree renderer ────────────────────────────────────────────────────

class TreeRenderer:
    """Renders a compact tree of events to a rich Console.

    Design: tool_start events are buffered. When tool_end arrives, the
    pair is merged into a single line. Text events flush pending tools.
    """

    def __init__(self, console: Console, label: str = ""):
        self._console = console
        self._label = label
        self._in_text = False
        self._pending: dict[str, RenderEvent] = {}

    def _ts(self, event: RenderEvent) -> str:
        """Format timestamp prefix, dim gray."""
        if event.ts:
            return f"[dim]{event.ts}[/dim] "
        return ""

    def render(self, event: RenderEvent) -> None:
        if event.kind == "thinking":
            return

        if event.kind == "tool_start":
            self._pending[event.tool_use_id] = event
            return

        if event.kind == "tool_end":
            start = self._pending.pop(event.tool_use_id, None)
            tool = event.tool or (start.tool if start else "?")
            icon = _tool_icon(tool)
            summary = (start.summary if start else "") or ""
            ts = self._ts(event)
            ok = event.summary != "\u5931\u8d25"
            result_tag = "[green]\u2714 \u6210\u529f[/green]" if ok else "[red]\u2718 \u5931\u8d25[/red]"
            prefix = "    \u251c\u2500 " if self._in_text else "  \u251c\u2500 "
            self._console.print(f"{prefix}{ts}{icon} [bold]{tool}[/bold]: {summary} \u2192 {result_tag}", highlight=False)
            return

        if event.kind == "text":
            self._flush_pending()
            self._in_text = True
            ts = self._ts(event)
            self._console.print(f"  {ts}\U0001f4ac {_truncate(event.summary, 100)}", highlight=False)
            return

        if event.kind == "cost":
            self._flush_pending()
            self._in_text = False
            ts = self._ts(event)
            self._console.print(f"  {ts}\U0001f4c8 [dim]{event.summary}[/dim]", highlight=False)
            return

        if event.kind == "rate_limit":
            ts = self._ts(event)
            self._console.print(f"  {ts}\u26a0\ufe0f  [yellow]\u9650\u901f: {event.summary}[/yellow]", highlight=False)
            return

        if event.kind == "session":
            ts = self._ts(event)
            self._console.print(f"  {ts}\u2699\ufe0f  [dim]{event.summary}[/dim]", highlight=False)
            return

        if event.kind == "prompt":
            ts = self._ts(event)
            self._console.print(f"  {ts}\U0001f4e8 [cyan]\u63d0\u793a\u8bcd[/cyan]: {_truncate(event.summary, 80)}", highlight=False)
            return

        if event.kind == "stop":
            self._flush_pending()
            self._in_text = False
            ts = self._ts(event)
            self._console.print(f"  {ts}\U0001f3c1 [dim]\u505c\u6b62: {_truncate(event.summary, 80)}[/dim]", highlight=False)
            return

    def _flush_pending(self) -> None:
        for tid, ev in list(self._pending.items()):
            icon = _tool_icon(ev.tool)
            ts = self._ts(ev)
            prefix = "    \u251c\u2500 " if self._in_text else "  \u251c\u2500 "
            self._console.print(f"{prefix}{ts}{icon} [bold]{ev.tool}[/bold]: {ev.summary} \u2192 [dim]\u7b49\u5f85\u4e2d[/dim]", highlight=False)
        self._pending.clear()
