#!/usr/bin/env python3
"""
gen_settings.py — Generate a session-specific settings.json with audit hooks only.

Produces a minimal settings.json containing only audit hooks. Does NOT inherit
the user's global ~/.claude/settings.json — Claude Code merges all setting
sources (flag, user, project, local) at runtime, so duplicating global settings
into flagSettings would cause hooks to execute twice and permissions to conflict.

Usage:
    python gen_settings.py generate --output /tmp/auditit/session-xxx/settings.json
    python gen_settings.py status   --settings /tmp/auditit/session-xxx/settings.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import HOOK_EVENTS

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "audit_hook.sh"


def generate(output: Path) -> None:
    """Create a minimal settings.json containing only audit hooks."""
    hooks: dict[str, list] = {}
    for event in HOOK_EVENTS:
        cmd = f"bash {HOOK_SCRIPT} {event}"
        hooks[event] = [{"hooks": [{"type": "command", "command": cmd}]}]

    settings = {"hooks": hooks}

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"[auditit] 已生成 session settings: {output}", file=sys.stderr)


def status(settings_path: Path) -> None:
    """Show whether a settings file contains audit hooks."""
    if not settings_path.exists():
        print(f"[auditit] 文件不存在: {settings_path}")
        return
    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})
    count = sum(
        1 for entries in hooks.values()
        for h in entries
        for hk in h.get("hooks", [])
        if "audit_hook.sh" in hk.get("command", "")
    )
    print(f"[auditit] {settings_path}: {count} audit hooks")


def main() -> None:
    parser = argparse.ArgumentParser(description="auditit settings generator")
    sub = parser.add_subparsers(dest="cmd")

    p_gen = sub.add_parser("generate", help="Generate session settings with audit hooks")
    p_gen.add_argument("--output", required=True, help="Output settings.json path")

    p_st = sub.add_parser("status", help="Check a settings file for audit hooks")
    p_st.add_argument("--settings", required=True, help="Settings file to check")

    args = parser.parse_args()
    if args.cmd == "generate":
        generate(Path(args.output))
    elif args.cmd == "status":
        status(Path(args.settings))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
