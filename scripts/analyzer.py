#!/usr/bin/env python3
"""
analyzer.py — Post-session analysis and report generation.

Reads audit.jsonl (+ optional stream.jsonl) from a session directory,
rebuilds the agent tree, and writes report.md.

Usage:
    python analyzer.py --session /tmp/auditit/session-xxx
    python analyzer.py --session /tmp/auditit/session-xxx --output custom_report.md
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import ctx_window, load_jsonl, read_transcript_meta, summarize_tool_input, truncate, ts_to_local


# ── Agent data model ──────────────────────────────────────────────────

@dataclass
class AgentReport:
    agent_id: str
    depth: int = 0
    parent_id: Optional[str] = None
    model: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    tool_counts: Counter = field(default_factory=Counter)
    tool_fail_counts: Counter = field(default_factory=Counter)
    # For duplicate-read detection
    files_read: list[str] = field(default_factory=list)
    # Timeline entries: (ts_str, description)
    timeline: list[tuple[str, str]] = field(default_factory=list)


# ── Builder ───────────────────────────────────────────────────────────

class SessionAnalyzer:
    def __init__(self, audit_events: list[dict]):
        self.events = audit_events
        self.agents: dict[str, AgentReport] = {}
        self.root_id: Optional[str] = None
        self.session_model: str = "unknown"
        self.total_turns: int = 0
        self.total_cost: float = 0.0
        self.total_duration_s: float = 0.0
        # Pending PreToolUse keyed by session_id
        self._pending: dict[str, dict] = {}
        # Map session_id → transcript_path for reading model/usage
        self._transcripts: dict[str, str] = {}

    def _ag(self, agent_id: str, depth: int = 0) -> AgentReport:
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentReport(agent_id=agent_id, depth=depth)
        return self.agents[agent_id]

    def build(self) -> None:
        for obj in self.events:
            event = obj.get("event", "")
            data  = obj.get("data", {})
            ts    = ts_to_local(obj.get("ts", ""))
            sid   = data.get("session_id", "")

            if event == "SessionStart":
                transcript = data.get("transcript_path", "")
                if sid and transcript:
                    self._transcripts[sid] = transcript
                if not self.root_id:
                    self.root_id = sid
                    ag = self._ag(sid, depth=0)
                ag = self._ag(sid)
                ag.timeline.append((ts, "SESSION START"))

            elif event == "UserPromptSubmit":
                ag = self._ag(sid)
                prompt = data.get("prompt", data.get("message", ""))
                if not isinstance(prompt, str):
                    prompt = json.dumps(prompt)
                ag.timeline.append((ts, f"USER: {truncate(prompt, 60)}"))

            elif event == "PreToolUse":
                tool = data.get("tool_name", "?")
                inp  = data.get("tool_input", {})
                self._pending[sid] = {"tool": tool, "input": inp, "ts": ts}

            elif event in ("PostToolUse", "PostToolUseFailure"):
                tool    = data.get("tool_name", "?")
                resp    = data.get("tool_response", data.get("tool_error", data.get("error", "")))
                failed  = event == "PostToolUseFailure"
                ag      = self._ag(sid)
                ag.tool_calls += 1
                ag.tool_counts[tool] += 1
                if failed:
                    ag.tool_failures += 1
                    ag.tool_fail_counts[tool] += 1

                pending = self._pending.pop(sid, {})
                inp     = pending.get("input", data.get("tool_input", {}))
                call_ts = pending.get("ts", ts)

                if tool == "Read" and isinstance(inp, dict):
                    fp = inp.get("file_path", "")
                    if fp:
                        ag.files_read.append(fp)

                status = "FAIL" if failed else "OK"
                summary = summarize_tool_input(tool, inp)
                ag.timeline.append((call_ts, f"{tool}: {summary}  [{status}]"))

            elif event == "Stop":
                ag = self._ag(sid)
                ag.turns += 1
                msg = data.get("last_assistant_message", "")
                if isinstance(msg, str) and msg:
                    ag.timeline.append((ts, f"STOP: {truncate(msg, 60)}"))

            elif event == "SubagentStart":
                # SubagentStart fires in parent session; agent_id = child's session_id
                parent_sid = sid
                child_sid  = data.get("agent_id", "")
                parent_depth = (
                    self.agents[parent_sid].depth
                    if parent_sid and parent_sid in self.agents
                    else 0
                )
                child_depth = parent_depth + 1
                child_ag = self._ag(child_sid, depth=child_depth)
                child_ag.depth = child_depth
                child_ag.timeline.append((ts, "SUBAGENT START"))

            elif event == "SubagentStop":
                child_sid = data.get("agent_id", sid)
                ag = self._ag(child_sid)
                ag.timeline.append((ts, "SUBAGENT STOP"))

            elif event == "SessionEnd":
                self.total_turns = sum(ag.turns for ag in self.agents.values())

        # Post-pass: read transcripts for model/usage data
        for sid, transcript in self._transcripts.items():
            ag = self.agents.get(sid)
            if not ag:
                continue
            meta = read_transcript_meta(transcript)
            if meta.get("model"):
                ag.model = meta["model"]
                if sid == self.root_id:
                    self.session_model = meta["model"]
            ag.input_tokens  = meta.get("input_tokens", 0)
            ag.output_tokens = meta.get("output_tokens", 0)
            ag.cache_tokens  = (
                meta.get("cache_read_input_tokens", 0)
                + meta.get("cache_creation_input_tokens", 0)
            )


# ── Report writer ─────────────────────────────────────────────────────

def generate_report(session_dir: Path, output_path: Optional[Path] = None) -> Path:
    audit_jsonl = session_dir / "audit.jsonl"
    if not audit_jsonl.exists():
        raise FileNotFoundError(f"No audit.jsonl found in {session_dir}")

    events = load_jsonl(audit_jsonl)
    analyzer = SessionAnalyzer(events)
    analyzer.build()

    if output_path is None:
        output_path = session_dir / "report.md"

    sections = [
        _section_overview(analyzer),
        _section_cost(analyzer),
        _section_tools(analyzer),
        _section_subagents(analyzer),
        _section_observations(analyzer),
        _section_timeline(analyzer),
    ]
    output_path.write_text("\n\n".join(s for s in sections if s) + "\n")
    return output_path


# ── Section builders ──────────────────────────────────────────────────

def _section_overview(a: SessionAnalyzer) -> str:
    total_tools = sum(ag.tool_calls for ag in a.agents.values())
    total_fails = sum(ag.tool_failures for ag in a.agents.values())
    fail_rate   = f"{int(100 * total_fails / total_tools)}%" if total_tools else "0%"
    n_sub = len([ag for ag in a.agents.values() if ag.depth > 0])

    root = a.agents.get(a.root_id or "", None)
    total_in = (root.input_tokens + root.cache_tokens) if root else 0
    ctx_lim  = ctx_window(a.session_model)
    ctx_pct  = int(100 * total_in / ctx_lim) if ctx_lim else 0

    lines = [
        "# Audit Report",
        "",
        f"| 项目 | 值 |",
        f"|------|---|",
        f"| Model | `{a.session_model}` |",
        f"| 耗时 | {a.total_duration_s:.1f}s |",
        f"| 轮次（root） | {a.total_turns} |",
        f"| Sub-agents | {n_sub} |",
        f"| 总工具调用 | {total_tools} |",
        f"| 工具失败率 | {fail_rate} |",
        f"| 总成本 | ${a.total_cost:.4f} |",
        f"| 上下文峰值 | {ctx_pct}% |",
    ]
    return "\n".join(lines)


def _section_cost(a: SessionAnalyzer) -> str:
    root = a.agents.get(a.root_id or "", None)
    if root is None:
        return ""

    inp   = root.input_tokens
    out   = root.output_tokens
    cache = root.cache_tokens
    total_in = inp + cache
    cache_pct = int(100 * cache / total_in) if total_in > 0 else 0

    lines = [
        "## Token & 成本",
        "",
        f"| 项目 | 值 |",
        f"|------|---|",
        f"| Input tokens（新） | {inp:,} |",
        f"| Input tokens（cache hit） | {cache:,} ({cache_pct}%) |",
        f"| Output tokens | {out:,} |",
        f"| 总成本 | ${a.total_cost:.4f} |",
    ]

    sub_agents = [ag for ag in a.agents.values() if ag.depth > 0]
    if sub_agents:
        sub_cost = sum(ag.cost_usd for ag in sub_agents)
        sub_pct  = int(100 * sub_cost / a.total_cost) if a.total_cost > 0 else 0
        lines.append(f"| Sub-agent 成本合计 | ${sub_cost:.4f} ({sub_pct}%) |")

    return "\n".join(lines)


def _section_tools(a: SessionAnalyzer) -> str:
    # Aggregate across all agents
    total_counts: Counter = Counter()
    total_fails:  Counter = Counter()
    for ag in a.agents.values():
        total_counts += ag.tool_counts
        total_fails  += ag.tool_fail_counts

    if not total_counts:
        return ""

    lines = [
        "## 工具调用统计（全 agent 树）",
        "",
        "| 工具 | 调用次数 | 失败次数 |",
        "|------|--------:|---------:|",
    ]
    for tool, count in total_counts.most_common():
        fails = total_fails.get(tool, 0)
        lines.append(f"| {tool} | {count} | {fails} |")

    # Duplicate-read detection
    dup_warnings: list[str] = []
    for ag in a.agents.values():
        counter = Counter(ag.files_read)
        for fp, n in counter.items():
            if n > 1:
                label = f"depth={ag.depth} {ag.agent_id[:8]}"
                dup_warnings.append(f"- `{fp}` 被同一 agent ({label}) Read {n} 次")

    if dup_warnings:
        lines += ["", "**重复读取（可优化）：**", ""] + dup_warnings

    return "\n".join(lines)


def _section_subagents(a: SessionAnalyzer) -> str:
    sub_agents = sorted(
        [ag for ag in a.agents.values() if ag.depth > 0],
        key=lambda ag: (ag.depth, ag.agent_id),
    )
    if not sub_agents:
        return ""

    lines = [
        "## Sub-agent 成本分解",
        "",
        "| Agent | Depth | Turns | 工具调用 | 失败 | 成本 |",
        "|-------|------:|------:|---------:|-----:|------|",
    ]

    root = a.agents.get(a.root_id or "", None)
    if root:
        lines.append(
            f"| root | 0 | {root.turns} | {root.tool_calls} | {root.tool_failures} | ${root.cost_usd:.4f} |"
        )
    for ag in sub_agents:
        lines.append(
            f"| {ag.agent_id[:12]} | {ag.depth} | {ag.turns} "
            f"| {ag.tool_calls} | {ag.tool_failures} | ${ag.cost_usd:.4f} |"
        )

    total_calls = sum(ag.tool_calls for ag in a.agents.values())
    total_fails_n = sum(ag.tool_failures for ag in a.agents.values())
    lines.append(
        f"| **合计** | - | {a.total_turns} "
        f"| {total_calls} | {total_fails_n} | **${a.total_cost:.4f}** |"
    )
    return "\n".join(lines)


def _section_observations(a: SessionAnalyzer) -> str:
    obs: list[str] = []

    total_tools = sum(ag.tool_calls for ag in a.agents.values())
    total_fails = sum(ag.tool_failures for ag in a.agents.values())

    if total_fails > 0:
        obs.append(f"工具调用失败 {total_fails} 次（共 {total_tools} 次）")

    root = a.agents.get(a.root_id or "", None)
    if root:
        total_in  = root.input_tokens + root.cache_tokens
        cache_pct = int(100 * root.cache_tokens / total_in) if total_in > 0 else 0
        ctx_lim   = ctx_window(a.session_model)
        ctx_pct   = int(100 * total_in / ctx_lim) if ctx_lim else 0

        if ctx_pct >= 85:
            obs.append(f"⚠  上下文压力极高 ({ctx_pct}%)，建议拆分任务")
        elif ctx_pct >= 70:
            obs.append(f"上下文压力较高 ({ctx_pct}%)")

        if cache_pct >= 60:
            obs.append(f"cache_hit 率高 ({cache_pct}%)，提示词结构良好")
        elif 0 < cache_pct < 20:
            obs.append(f"cache_hit 率低 ({cache_pct}%)，考虑优化提示词")

    # Duplicate reads
    for ag in a.agents.values():
        counter = Counter(ag.files_read)
        dups = [fp for fp, n in counter.items() if n > 1]
        if dups:
            obs.append(f"agent {ag.agent_id[:8]} 重复读取 {len(dups)} 个文件")

    if not obs:
        return ""

    lines = ["## 优化建议", ""]
    for o in obs:
        lines.append(f"- {o}")
    return "\n".join(lines)


def _section_timeline(a: SessionAnalyzer, max_entries: int = 80) -> str:
    # Merge timelines from all agents, tag with depth
    entries: list[tuple[str, int, str]] = []
    for ag in a.agents.values():
        for ts, desc in ag.timeline:
            entries.append((ts, ag.depth, desc))

    entries.sort(key=lambda x: x[0])
    entries = entries[:max_entries]

    if not entries:
        return ""

    lines = ["## Timeline", "", "| 时间 | Depth | 事件 |", "|------|------:|------|"]
    for ts, depth, desc in entries:
        indent = "　" * depth
        lines.append(f"| {ts} | {depth} | {indent}{desc} |")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="auditit session analyzer")
    parser.add_argument("--session", required=True, help="Session directory path")
    parser.add_argument("--output",  help="Output report path (default: session/report.md)")
    args = parser.parse_args()

    session_dir = Path(args.session)
    output_path = Path(args.output) if args.output else None

    report_path = generate_report(session_dir, output_path)
    print(f"[auditit] 报告已生成: {report_path}")


if __name__ == "__main__":
    main()
