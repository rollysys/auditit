#!/usr/bin/env python3
"""
worker.py — auditit single-prompt worker.

Runs `claude -p --output-format stream-json --verbose` as a subprocess,
parses events in real-time, and renders them with rich using TreeRenderer.

Usage:
    python3 worker.py --prompt "..." [--model sonnet] [--max-turns 100]
                      [--repo-root .] [--session-dir /tmp/auditit/session-xxx]
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console

from render_events import TreeRenderer, parse_stream_line
from util import ensure_dir, write_json


def _run(
    prompt: str,
    model: str,
    max_turns: int,
    repo_root: Path,
    session_dir: Path,
    audit_settings: Path | None,
    console: Console,
) -> None:
    ensure_dir(session_dir)
    stream_jsonl = session_dir / "stream.jsonl"

    # Use auditit session settings (includes audit hooks)
    # Fall back to a bare settings file if not provided
    if audit_settings and audit_settings.exists():
        settings_path = audit_settings
    else:
        settings_path = session_dir / "settings.json"
        settings_path.write_text(json.dumps({
            "permissions": {"defaultMode": "bypassPermissions"},
        }, indent=2) + "\n")

    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--no-chrome",
        "--max-turns", str(max_turns),
        "--settings", str(settings_path),
        "--", prompt,
    ]

    console.rule(f"[bold cyan]{prompt[:60]}[/bold cyan]")

    env = os.environ.copy()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=env, cwd=str(repo_root),
    )

    event_queue: queue.Queue = queue.Queue()
    all_lines: list[str] = []

    def read_stream():
        assert proc.stdout is not None
        for line in proc.stdout:
            all_lines.append(line)
            for ev in parse_stream_line(line):
                event_queue.put(ev)

    thread = threading.Thread(target=read_stream, daemon=True)
    thread.start()

    renderer = TreeRenderer(console, label=prompt[:40])
    seen_ids: set[str] = set()

    while True:
        if proc.poll() is not None:
            time.sleep(0.2)
            while not event_queue.empty():
                try:
                    ev = event_queue.get_nowait()
                    if ev.tool_use_id and ev.tool_use_id in seen_ids and ev.kind == "tool_start":
                        continue
                    if ev.tool_use_id:
                        seen_ids.add(ev.tool_use_id)
                    renderer.render(ev)
                except queue.Empty:
                    break
            break

        try:
            ev = event_queue.get(timeout=0.3)
        except queue.Empty:
            continue

        if ev.kind == "tool_start" and ev.tool_use_id:
            if ev.tool_use_id in seen_ids:
                continue
            seen_ids.add(ev.tool_use_id)
        elif ev.tool_use_id:
            seen_ids.add(ev.tool_use_id)

        renderer.render(ev)

    thread.join(timeout=3)
    stream_jsonl.write_text("".join(all_lines))
    console.print(f"\n  [dim]stream log → {stream_jsonl}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="auditit single-prompt worker")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt",       help="Prompt string")
    group.add_argument("--prompt-file",  help="Read prompt from file (avoids shell escaping issues)")
    parser.add_argument("--model",       default="sonnet")
    parser.add_argument("--max-turns",   type=int, default=100)
    parser.add_argument("--repo-root",   default=".")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--audit-settings", help="Session settings.json with audit hooks")
    args = parser.parse_args()

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()

    audit_settings = Path(args.audit_settings) if args.audit_settings else None

    console = Console(force_terminal=True)
    _run(
        prompt=prompt,
        model=args.model,
        max_turns=args.max_turns,
        repo_root=Path(args.repo_root).resolve(),
        session_dir=Path(args.session_dir),
        audit_settings=audit_settings,
        console=console,
    )


if __name__ == "__main__":
    main()
