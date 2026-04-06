#!/usr/bin/env python3
"""
display.py — auditit real-time display engine.

Tails audit.jsonl and renders a live, tree-structured view of Claude Code
session activity, including nested sub-agents at correct indentation depth.

Usage:
    python display.py --follow /tmp/auditit/session-xxx/audit.jsonl
    python display.py --replay /path/to/audit.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import (
    ctx_window,
    now_ts,
    read_transcript_meta,
    summarize_tool_input,
    summarize_tool_response,
    truncate,
    ts_to_local,
)

try:
    from rich.console import Console
    from rich.text import Text
    RICH = True
except ImportError:
    RICH = False

console = Console(force_terminal=True) if RICH else None

# ── Tool icons ────────────────────────────────────────────────────────

TOOL_ICONS: dict[str, str] = {
    "Read":         "📖",
    "Write":        "✏️ ",
    "Edit":         "✏️ ",
    "MultiEdit":    "✏️ ",
    "NotebookEdit": "✏️ ",
    "Grep":         "🔍",
    "Glob":         "🔍",
    "Bash":         "🔧",
    "Agent":        "🤖",
    "Task":         "📋",
    "WebSearch":    "🌐",
    "WebFetch":     "🌐",
}

def tool_icon(name: str) -> str:
    return TOOL_ICONS.get(name, "🛠️ ")


# ── Agent state ───────────────────────────────────────────────────────

@dataclass
class AgentState:
    agent_id: str
    depth: int = 0
    parent_id: Optional[str] = None
    model: str = ""
    # Metrics
    tool_calls: int = 0
    tool_failures: int = 0
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    # Lifecycle
    started_at: str = ""
    stopped: bool = False
    # Pending tool: buffer PreToolUse until PostToolUse arrives
    pending_tool: Optional[dict] = field(default=None, repr=False)


# ── Renderer ─────────────────────────────────────────────────────────

class AuditDisplay:
    def __init__(self, compact: bool = False, filter_sid: str = ""):
        self.compact = compact
        self.agents: dict[str, AgentState] = {}
        self.root_agent_id: Optional[str] = None
        self.session_model: str = "unknown"
        self.session_base_url: str = ""
        self.session_started: bool = False
        self.total_cost: float = 0.0
        self.session_ended: bool = False
        self._transcripts: dict[str, str] = {}  # session_id → transcript_path
        # Optional filter: only render events from this session and its sub-agents
        self._filter_sid = filter_sid
        self._allowed_sids: set[str] = {filter_sid} if filter_sid else set()

    # ── Agent lookup ─────────────────────────────────────────────────

    def _agent(self, agent_id: str, depth: int = 0) -> AgentState:
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentState(agent_id=agent_id, depth=depth)
        return self.agents[agent_id]

    # ── Visual prefix helpers ─────────────────────────────────────────

    @staticmethod
    def _pipe(depth: int) -> str:
        """Vertical bars for sub-agent nesting."""
        return "│  " * depth

    # ── Output ───────────────────────────────────────────────────────

    def _print(self, text: str, *, markup: bool = True) -> None:
        if RICH:
            console.print(text, highlight=False, markup=markup)
        else:
            print(re.sub(r"\[/?[^\]]*\]", "", text))

    def _ctx_str(self, ag: AgentState) -> str:
        """Return a colored ctx% string for an agent, or '' if no token data."""
        total_in = ag.input_tokens + ag.cache_tokens
        if not total_in:
            return ""
        ctx_lim   = ctx_window(ag.model or self.session_model)
        ctx_pct   = int(100 * total_in / ctx_lim)
        ctx_color = "green" if ctx_pct < 70 else ("yellow" if ctx_pct < 85 else "red")
        return f"[{ctx_color}]📊 {ctx_pct}%[/{ctx_color}]"

    def _detail_block(self, text: str, depth: int = 0, max_lines: int = 0) -> None:
        """Print assistant output as rendered Markdown."""
        if not text or not text.strip():
            return
        if self.compact:
            return  # start mode: interactive pane shows full output
        pipe = self._pipe(depth)
        indent = f"         {pipe}   "
        if RICH:
            from rich.markdown import Markdown
            from rich.padding import Padding
            md = Markdown(text.strip(), code_theme="monokai")
            padded = Padding(md, (0, 0, 0, len(indent)))
            console.print(padded)
        else:
            lines = text.strip().splitlines()
            show = lines[:max_lines] if max_lines > 0 else lines
            for ln in show:
                print(f"{indent}{ln}")
            if max_lines > 0 and len(lines) > max_lines:
                print(f"{indent}... +{len(lines) - max_lines} lines")

    def _status_line(self) -> None:
        """Print a compact live status line (after Stop events)."""
        total_calls = sum(ag.tool_calls for ag in self.agents.values())
        total_fails = sum(ag.tool_failures for ag in self.agents.values())
        n_sub = sum(1 for ag in self.agents.values() if ag.depth > 0)

        parts = [f"[bold]💰 ${self.total_cost:.4f}[/bold]"]
        if n_sub:
            parts.append(f"[dim]sub-agents:{n_sub}[/dim]")
        parts.append(f"[dim]tools:{total_calls}[/dim]")
        if total_fails:
            parts.append(f"[red]fails:{total_fails}[/red]")
        self._print("         " + "  │  ".join(parts))

    # ── Event processing ──────────────────────────────────────────────

    def process_line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return
        self._dispatch(obj)

    def _sid_allowed(self, sid: str) -> bool:
        """Return True if sid belongs to the filtered session tree (or no filter set)."""
        if not self._filter_sid:
            return True
        if not sid:
            return False
        return any(sid.startswith(a) or a.startswith(sid) for a in self._allowed_sids)

    def _dispatch(self, obj: dict) -> None:
        event    = obj.get("event", "")
        data     = obj.get("data", {})
        ts       = ts_to_local(obj.get("ts", ""))
        base_url = obj.get("base_url", "")

        # Attach base_url from envelope so handlers can access it
        if base_url:
            data["_base_url"] = base_url

        if self._filter_sid:
            sid = data.get("session_id", "")
            # SubagentStart: if parent is allowed, add child to the allowed set
            if event == "SubagentStart":
                child_sid = data.get("agent_id", "")
                if self._sid_allowed(sid):
                    self._allowed_sids.add(child_sid)
                else:
                    return
            elif not self._sid_allowed(sid):
                return

        handler = getattr(self, f"_on_{event.lower()}", None)
        if handler:
            handler(ts, data)
        # Silently ignore unknown events

    # ── Individual event handlers ─────────────────────────────────────

    def _on_sessionstart(self, ts: str, data: dict) -> None:
        sid = data.get("session_id", "root")
        transcript = data.get("transcript_path", "")
        base_url = data.get("_base_url", "")

        ag = self._agent(sid, depth=0)
        if transcript:
            ag.model = ""  # will be populated from transcript on SessionEnd
            self._transcripts[sid] = transcript

        if not self.root_agent_id:
            self.root_agent_id = sid
            ag.started_at = ts
            if base_url:
                self.session_base_url = base_url

        if not self.session_started:
            self.session_started = True

        line = f"[dim]{ts}[/dim]  ⚙  [bold]SESSION START[/bold]  [dim]{sid[:12]}[/dim]"
        if base_url:
            line += f"  [dim]api={base_url}[/dim]"
        self._print(line)

    def _on_userpromptsubmit(self, ts: str, data: dict) -> None:
        prompt = data.get("prompt", data.get("message", ""))
        if not isinstance(prompt, str):
            prompt = json.dumps(prompt, ensure_ascii=False)
        preview = truncate(prompt.replace("\n", " "), 80)
        sid = data.get("session_id", self.root_agent_id or "")
        depth = self.agents[sid].depth if sid in self.agents else 0
        pipe = self._pipe(depth)
        self._print(f"[dim]{ts}[/dim]  {pipe}👤 [bold]USER[/bold]  [dim]│[/dim]  {preview}")

    def _on_pretooluse(self, ts: str, data: dict) -> None:
        tool    = data.get("tool_name", "?")
        inp     = data.get("tool_input", {})
        sid = data.get("session_id", self.root_agent_id or "")
        ag = self._agent(sid)
        # Buffer — render when PostToolUse arrives
        ag.pending_tool = {"tool": tool, "input": inp, "ts": ts}

    def _on_posttooluse(self, ts: str, data: dict) -> None:
        self._render_tool(ts, data, failed=False)

    def _on_posttoolusefailure(self, ts: str, data: dict) -> None:
        self._render_tool(ts, data, failed=True)

    def _render_tool(self, ts: str, data: dict, failed: bool) -> None:
        tool     = data.get("tool_name", "?")
        resp     = data.get("tool_response", data.get("tool_error", data.get("error", "")))
        sid = data.get("session_id", self.root_agent_id or "")
        ag = self._agent(sid)
        ag.tool_calls += 1
        if failed:
            ag.tool_failures += 1

        pending  = ag.pending_tool or {}
        inp      = pending.get("input", data.get("tool_input", {}))
        call_ts  = pending.get("ts", ts)
        ag.pending_tool = None

        icon     = tool_icon(tool)
        summary  = truncate(summarize_tool_input(tool, inp), 70)
        resp_sum = truncate(summarize_tool_response(tool, resp), 70)
        depth    = ag.depth
        pipe     = self._pipe(depth)
        status   = "[red]✘[/red]" if failed else "[green]✔[/green]"

        line = f"[dim]{call_ts}[/dim]  {pipe}{icon} [bold]{tool}[/bold]"
        if summary:
            line += f"  [dim]{summary}[/dim]"
        line += f"  →  {status}"
        if resp_sum and not failed:
            line += f"  [dim]{resp_sum}[/dim]"
        elif failed and resp_sum:
            line += f"  [red]{resp_sum}[/red]"

        self._print(line)

    def _on_stop(self, ts: str, data: dict) -> None:
        sid = data.get("session_id", self.root_agent_id or "")
        ag = self._agent(sid)
        ag.turns += 1

        msg = data.get("last_assistant_message", "")
        if not isinstance(msg, str):
            msg = ""

        depth = ag.depth
        pipe  = self._pipe(depth)

        # One-line header
        first_line = truncate(msg.replace("\n", " "), 60)
        self._print(f"[dim]{ts}[/dim]  {pipe}🏁 [bold]STOP[/bold]  [dim]│[/dim]  {first_line}")
        # Full output in dim detail block
        self._detail_block(msg, depth)
        self._status_line()

    def _on_subagentstart(self, ts: str, data: dict) -> None:
        # SubagentStart fires in the parent session context
        parent_sid = data.get("session_id", "")
        child_sid  = data.get("agent_id", "")  # child's session_id, named agent_id in event
        if not child_sid:
            return

        parent_depth = (
            self.agents[parent_sid].depth
            if parent_sid and parent_sid in self.agents
            else 0
        )
        child_depth = parent_depth + 1

        child_ag = self._agent(child_sid, depth=child_depth)
        child_ag.depth = child_depth

        pipe = self._pipe(parent_depth)
        desc = data.get("description", "")
        desc_part = f"  [dim]{truncate(desc, 40)}[/dim]" if desc else ""

        self._print(
            f"[dim]{ts}[/dim]  {pipe}[bold yellow]┌ 🤖 SUBAGENT[/bold yellow]  "
            f"[dim]depth={child_depth}  id={child_sid[:12]}[/dim]{desc_part}"
        )

    def _on_subagentstop(self, ts: str, data: dict) -> None:
        child_sid = data.get("agent_id", "")
        ag = self._agent(child_sid) if child_sid else None

        child_depth  = ag.depth if ag else 1
        parent_depth = max(child_depth - 1, 0)

        msg = data.get("last_assistant_message", "")
        if not isinstance(msg, str):
            msg = ""
        first_line = truncate(msg.replace("\n", " "), 50)

        if ag:
            ag.stopped = True
            turns_s = f"  [dim]turns={ag.turns}[/dim]" if ag.turns else ""
            cost_s  = f"  [dim]${ag.cost_usd:.4f}[/dim]" if ag.cost_usd else ""
            _cs     = self._ctx_str(ag)
            ctx_s   = f"  {_cs}" if _cs else ""
        else:
            turns_s = cost_s = ctx_s = ""

        pipe = self._pipe(parent_depth)
        self._print(
            f"[dim]{ts}[/dim]  {pipe}[bold yellow]└ 🤖 SUBAGENT STOP[/bold yellow]  "
            f"[green]✔[/green]{turns_s}{cost_s}{ctx_s}  [dim]{first_line}[/dim]"
        )
        # Full sub-agent output in dim detail block
        self._detail_block(msg, child_depth)

    def _on_sessionend(self, ts: str, data: dict) -> None:
        sid = data.get("session_id", "")

        # Read transcript for model/usage (not available in hook data)
        transcript = data.get("transcript_path", "") or self._transcripts.get(sid, "")
        if transcript:
            meta = read_transcript_meta(transcript)
            ag = self._agent(sid)
            if meta.get("model"):
                ag.model = meta["model"]
                if sid == self.root_agent_id:
                    self.session_model = meta["model"]
            ag.input_tokens  = meta.get("input_tokens", 0)
            ag.output_tokens = meta.get("output_tokens", 0)
            ag.cache_tokens  = (
                meta.get("cache_read_input_tokens", 0)
                + meta.get("cache_creation_input_tokens", 0)
            )

        # Only show summary and set exit flag for the root session
        if sid == self.root_agent_id:
            self._print_summary(data)
            self.session_ended = True

    def _on_notification(self, ts: str, data: dict) -> None:
        msg = data.get("message", str(data))
        self._print(f"[dim]{ts}[/dim]  🔔 [dim]{truncate(msg, 80)}[/dim]")

    def _on_taskcompleted(self, ts: str, data: dict) -> None:
        result = data.get("result", "")
        self._print(f"[dim]{ts}[/dim]  ✅ [bold]TASK COMPLETED[/bold]  [dim]{truncate(str(result), 60)}[/dim]")

    # ── Summary ───────────────────────────────────────────────────────

    def _print_summary(self, data: dict) -> None:
        total_calls  = sum(ag.tool_calls    for ag in self.agents.values())
        total_fails  = sum(ag.tool_failures for ag in self.agents.values())
        total_turns  = sum(ag.turns for ag in self.agents.values())

        # Aggregate token data from all agents (populated from transcripts)
        root_ag = self.agents.get(self.root_agent_id or "", None)
        inp   = root_ag.input_tokens  if root_ag else 0
        out   = root_ag.output_tokens if root_ag else 0
        cache = root_ag.cache_tokens  if root_ag else 0
        total_in  = inp + cache
        cache_pct = int(100 * cache / total_in) if total_in > 0 else 0

        ctx_lim = ctx_window(self.session_model)
        ctx_pct = int(100 * total_in / ctx_lim) if ctx_lim else 0
        ctx_color = "green" if ctx_pct < 70 else ("yellow" if ctx_pct < 85 else "red")

        sub_agents = sorted(
            [ag for ag in self.agents.values() if ag.depth > 0],
            key=lambda a: (a.depth, a.agent_id),
        )

        # Single-line summary
        parts = [
            f"[bold]SUMMARY[/bold]",
            f"turns={total_turns}",
            f"tools={total_calls}",
        ]
        if total_fails:
            parts.append(f"[red]fails={total_fails}[/red]")
        parts.append(f"ctx=[{ctx_color}]{ctx_pct}%[/{ctx_color}]")
        parts.append(f"in={inp:,}+cache={cache:,}({cache_pct}%)")
        parts.append(f"out={out:,}")
        if sub_agents:
            parts.append(f"sub-agents={len(sub_agents)}")
        if self.session_base_url:
            parts.append(f"[dim]api={self.session_base_url}[/dim]")

        self._print(f"📊 " + "  │  ".join(parts))


# ── File tailing ──────────────────────────────────────────────────────

def _wait_for_file(path: Path, timeout_s: int = 600) -> bool:
    """Block until path exists or timeout."""
    deadline = time.time() + timeout_s
    while not path.exists():
        if time.time() > deadline:
            return False
        time.sleep(0.3)
    return True


def follow(path: Path, display: AuditDisplay) -> None:
    """Tail audit.jsonl, processing new lines as they appear."""
    if RICH:
        console.print(f"[dim]auditit: 等待事件 … ({path})[/dim]")
    else:
        print(f"auditit: waiting for events ... ({path})")

    if not _wait_for_file(path):
        print("auditit: 超时，未检测到会话启动", file=sys.stderr)
        return

    with open(path) as fh:
        idle_after_end = 0.0
        while True:
            line = fh.readline()
            if line:
                display.process_line(line)
                idle_after_end = 0.0
            else:
                time.sleep(0.05)
                if display.session_ended:
                    idle_after_end += 0.05
                    if idle_after_end >= 2.0:
                        break  # graceful exit after SessionEnd + 2s drain


def replay(path: Path, display: AuditDisplay, delay: float = 0.0) -> None:
    """Replay an existing audit log from beginning."""
    with open(path) as fh:
        for line in fh:
            display.process_line(line)
            if delay:
                time.sleep(delay)


# ── Entry point ───────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="auditit display engine")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--follow", metavar="PATH", help="Audit log to tail (waits for file)")
    grp.add_argument("--replay", metavar="PATH", help="Audit log to replay offline")
    parser.add_argument("--delay", type=float, default=0.02,
                        help="Per-line delay when replaying (default 0.02s)")
    parser.add_argument("--compact", action="store_true",
                        help="Truncate agent output (for start mode where interactive pane shows full output)")
    parser.add_argument("--session-id", metavar="SID", default="",
                        help="Only show events from this session (prefix match). Filters out unrelated sessions sharing the same audit.jsonl.")
    args = parser.parse_args()

    if not RICH:
        print("Warning: 'rich' not installed — output will be plain text.", file=sys.stderr)
        print("Install with: pip install rich", file=sys.stderr)

    display = AuditDisplay(compact=args.compact, filter_sid=args.session_id)

    if args.replay:
        replay(Path(args.replay), display, delay=args.delay)
    else:
        follow(Path(args.follow), display)


if __name__ == "__main__":
    main()
