#!/usr/bin/env python3
"""migrate_flatten.py — one-shot migration: date-partitioned → flat layout.

Old layout: ~/.claude-audit/YYYY-MM-DD/<sid>/audit.jsonl(.gz)
New layout: ~/.claude-audit/<sid>/audit.jsonl(.gz)

Long-running sessions used to fragment into one dir per UTC day they were
active, producing N "phantom" entries in the Sessions list. Flat layout
removes the date partition entirely. This script consolidates existing
data:

  1. For each <date>/<sid>/, merge into ~/.claude-audit/<sid>/.
  2. If the destination already exists (multi-day session), merge audit
     events by ts and pick the right summary/metadata/env files.
  3. Original date-partitioned dirs are KEPT as backup; remove them
     manually after verifying the migration looks correct.

Run:  python3 migrate_flatten.py [--dry-run] [--audit-dir PATH]

Idempotent — re-running on already-flat data is a no-op.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

DATE_RE_LEN = 10  # "YYYY-MM-DD"


def is_date_dir(name: str) -> bool:
    return (
        len(name) == DATE_RE_LEN
        and name.startswith("20")
        and name[4] == "-"
        and name[7] == "-"
    )


def discover(audit_dir: Path) -> dict[str, list[Path]]:
    """Return {sid: [old_dir1, old_dir2, ...]} sorted by date asc."""
    by_sid: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    if not audit_dir.exists():
        return {}
    for date_dir in audit_dir.iterdir():
        if not date_dir.is_dir() or not is_date_dir(date_dir.name):
            continue
        for sid_dir in date_dir.iterdir():
            if not sid_dir.is_dir():
                continue
            by_sid[sid_dir.name].append((date_dir.name, sid_dir))
    return {sid: [p for _, p in sorted(parts)] for sid, parts in by_sid.items()}


def read_events_sorted(jsonls: list[Path]) -> list[bytes]:
    """Read all event lines from given audit.jsonl(.gz) paths, sorted by ts.

    Dedupes by (ts, event, data) so re-running the migration over already-
    consolidated data is idempotent. Without dedup, "merge sources + dst"
    on a second run would double every event.
    """
    rows: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for path in jsonls:
        if not path.exists():
            continue
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rb") as f:
                for line in f:
                    if not line.strip():
                        continue
                    # Lines with bad UTF-8 bytes still need to be preserved —
                    # parse only to extract the ts; if even that fails, sort
                    # them as ts="" (front of the stream) but keep the raw
                    # bytes intact for the merged output.
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        rows.append(("", line))
                        continue
                    # Dedupe key — canonical JSON of the relevant fields.
                    try:
                        key = json.dumps(
                            (obj.get("ts", ""), obj.get("event", ""), obj.get("data", {})),
                            sort_keys=True, ensure_ascii=False,
                        )
                    except (TypeError, ValueError):
                        key = ""
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    rows.append((obj.get("ts", "") or "", line))
        except OSError:
            continue
    rows.sort(key=lambda x: x[0])
    return [r[1] for r in rows]


def merge_metadata(srcs: list[Path]) -> dict | None:
    """Earliest started_at + latest model + first non-empty cwd/prompt."""
    out: dict = {}
    started: list[str] = []
    for src in srcs:
        p = src / "metadata.json"
        if not p.exists():
            continue
        try:
            with open(p) as f:
                m = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            continue
        if not out:
            out = dict(m)
        if m.get("started_at"):
            started.append(m["started_at"])
        for k in ("prompt", "model", "cwd"):
            if not out.get(k) and m.get(k):
                out[k] = m[k]
    if started:
        out["started_at"] = min(started)
    return out or None


def pick_first_existing(srcs: list[Path], name: str) -> Path | None:
    for src in srcs:
        p = src / name
        if p.exists():
            return p
    return None


def pick_last_existing(srcs: list[Path], name: str) -> Path | None:
    found = None
    for src in srcs:
        p = src / name
        if p.exists():
            found = p
    return found


def write_atomic(dst: Path, data: bytes) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=".mig.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise


def consolidate_one(sid: str, srcs: list[Path], audit_dir: Path, dry_run: bool) -> tuple[str, str]:
    """Return (sid, action_summary)."""
    dst = audit_dir / sid
    actions: list[str] = []

    # Audit events: merge from all source dirs into one stream
    jsonls: list[Path] = []
    for src in srcs:
        for fn in ("audit.jsonl", "audit.jsonl.gz"):
            p = src / fn
            if p.exists():
                jsonls.append(p)
    # Plus any pre-existing dst content (idempotent re-runs)
    if dst.exists():
        for fn in ("audit.jsonl", "audit.jsonl.gz"):
            p = dst / fn
            if p.exists():
                jsonls.append(p)

    merged = read_events_sorted(jsonls) if jsonls else []
    if merged:
        # Always write as plain audit.jsonl. SessionEnd will gzip later.
        # Sub-agent dirs must be migrated separately (this function only
        # handles the named SID — sub-agent dirs are siblings under each
        # date and are migrated as their own SID entries).
        body = b"".join(merged)
        actions.append(f"merged {len(merged)} events from {len(jsonls)} jsonl(s)")
        if not dry_run:
            # If dst already had a .gz from a prior partial migration,
            # remove it; we will re-emit as plain or .gz as appropriate.
            for old in (dst / "audit.jsonl", dst / "audit.jsonl.gz"):
                if old.exists():
                    try: old.unlink()
                    except OSError: pass
            # If any source was .gz (i.e. SessionEnd happened), emit .gz.
            any_gz = any(p.name.endswith(".gz") for p in jsonls)
            if any_gz:
                fd, tmp = tempfile.mkstemp(dir=str(dst.parent if dst.exists() else audit_dir),
                                            prefix=".mig.", suffix=".gz")
                try:
                    with gzip.open(tmp, "wb", compresslevel=6) as f:
                        f.write(body)
                    dst.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, dst / "audit.jsonl.gz")
                except Exception:
                    try: os.remove(tmp)
                    except OSError: pass
                    raise
            else:
                write_atomic(dst / "audit.jsonl", body)

    # summary.json: pick the LAST one (SessionEnd output); falls back to None
    summary_src = pick_last_existing(srcs, "summary.json")
    if summary_src and not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        write_atomic(dst / "summary.json", summary_src.read_bytes())
    if summary_src:
        actions.append(f"summary from {summary_src.parent.name}")

    # metadata.json: merge across all sources
    meta = merge_metadata(srcs)
    if meta and not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        write_atomic(dst / "metadata.json", json.dumps(meta, indent=2).encode())
    if meta:
        actions.append("metadata merged")

    # env.json: pick the FIRST one (closest to true session origin)
    env_src = pick_first_existing(srcs, "env.json")
    if env_src and not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        write_atomic(dst / "env.json", env_src.read_bytes())
    if env_src:
        actions.append(f"env from {env_src.parent.name}")

    # meta.json (sub-agent layers carry these)
    sub_meta_src = pick_first_existing(srcs, "meta.json")
    if sub_meta_src and not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        write_atomic(dst / "meta.json", sub_meta_src.read_bytes())
    if sub_meta_src:
        actions.append("meta (sub-agent) copied")

    return (sid, " · ".join(actions) if actions else "nothing to do")


def dedupe_flat(audit_dir: Path, dry_run: bool) -> int:
    """Walk every flat session dir and dedupe its audit.jsonl(.gz).

    Repairs the side effect of running the migration twice in succession:
    an earlier bug re-included the destination's content as a "source"
    on subsequent runs, so every event was appended a second time.
    Idempotent — re-running on already-clean data is a no-op.
    """
    fixed = 0
    scanned = 0
    for sd in sorted(audit_dir.iterdir()):
        if not sd.is_dir():
            continue
        name = sd.name
        if name.startswith("_") or name.startswith("."):
            continue
        if is_date_dir(name):
            continue  # date-partitioned dir handled by main migration path
        sources: list[Path] = []
        for fn in ("audit.jsonl.gz", "audit.jsonl"):
            p = sd / fn
            if p.exists():
                sources.append(p)
        if not sources:
            continue
        scanned += 1
        # Count before
        original_total = 0
        for p in sources:
            opener = gzip.open if p.name.endswith(".gz") else open
            try:
                with opener(p, "rb") as f:
                    original_total += sum(1 for L in f if L.strip())
            except OSError:
                pass
        deduped_lines = read_events_sorted(sources)
        if len(deduped_lines) == original_total:
            continue  # no duplicates, nothing to do
        fixed += 1
        delta = original_total - len(deduped_lines)
        print(f"  {name[:8]}…  {original_total} → {len(deduped_lines)}  (-{delta} dup)")
        if dry_run:
            continue
        body = b"".join(deduped_lines)
        # Always rewrite to the same shape as currently exists. If both
        # exist we collapse into the .gz (the .jsonl came from a resume
        # AFTER the duplicate poisoning, but its events are already in
        # the deduped stream, so we can drop the plain file).
        any_gz = any(p.name.endswith(".gz") for p in sources)
        target_gz = sd / "audit.jsonl.gz"
        target_jl = sd / "audit.jsonl"
        if any_gz:
            tmp = sd / "audit.jsonl.gz.tmp"
            try:
                with gzip.open(tmp, "wb", compresslevel=6) as f:
                    f.write(body)
                os.replace(tmp, target_gz)
                if target_jl.exists():
                    target_jl.unlink()
            except Exception as e:
                print(f"    FAIL writing gz: {e}")
                try: tmp.unlink()
                except OSError: pass
        else:
            write_atomic(target_jl, body)

    print()
    print(f"  scanned {scanned} session dir(s); rewrote {fixed} with dedup")
    return 0


# ── Scripted-prompt patterns ─────────────────────────────────────────
#
# Historical sessions were recorded before hook.py captured parent_cmd,
# so we cannot determine is_headless from env directly. Instead we match
# the first UserPromptSubmit's prompt text against high-precision
# markers that only appear in automated / scripted invocations.
#
# All patterns are DIRECT substring matches (case-sensitive). Keep them
# specific; false positives retroactively hide real conversations from
# the default view. The UI toggle recovers them but still: be conservative.
SCRIPTED_PROMPT_MARKERS: tuple[str, ...] = (
    "自动化流水线任务",
    "重要：这是自动化",
    "重要：直接执行",
    "直接执行，不需要确认",
    "你是 AA-",
    "你是估值分析 agent",
    "你是情景分析",
    "你是研究问题生成器",
    "你是一个金融研究 sub-agent",
)


def _first_user_prompt(session_dir: Path) -> str:
    """Return the first UserPromptSubmit's prompt text; '' if not found."""
    for name in ("audit.jsonl", "audit.jsonl.gz"):
        p = session_dir / name
        if not p.exists():
            continue
        opener = gzip.open if name.endswith(".gz") else open
        try:
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                for L in f:
                    if not L.strip():
                        continue
                    try:
                        o = json.loads(L)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if o.get("event") == "UserPromptSubmit":
                        return (o.get("data", {}) or {}).get("prompt", "") or ""
        except OSError:
            continue
    return ""


def _matches_scripted(prompt: str) -> str:
    """Return the matching marker string, or '' if no match."""
    if not prompt:
        return ""
    for m in SCRIPTED_PROMPT_MARKERS:
        if m in prompt:
            return m
    return ""


def backfill_mode(audit_dir: Path, dry_run: bool) -> int:
    """Mark historical scripted sessions via prompt-pattern inference.

    For each flat session dir without a live is_headless signal, read
    the first UserPromptSubmit and check for known scripted markers.
    On match, write is_headless=true into env.json plus a _backfill_source
    / _backfill_marker trail for audit.

    Live signals (is_headless written by hook.py from parent_cmd) always
    win over retroactive inference — those sessions are skipped.
    Idempotent.
    """
    marked = 0
    scanned = 0
    already = 0
    skipped = 0
    for sd in sorted(audit_dir.iterdir()):
        if not sd.is_dir():
            continue
        name = sd.name
        if name.startswith("_") or name.startswith("."):
            continue
        if is_date_dir(name):
            continue
        if "__agent__" in name:
            # Sub-agents inherit from their root; do not classify
            # independently.
            continue
        scanned += 1
        env_path = sd / "env.json"
        env_cur: dict = {}
        if env_path.exists():
            try:
                with open(env_path) as f:
                    env_cur = json.load(f) or {}
            except (OSError, json.JSONDecodeError):
                env_cur = {}
        # Live signal wins; never overwrite a hook-captured classification.
        if "is_headless" in env_cur and "_backfill_source" not in env_cur:
            already += 1
            continue
        prompt = _first_user_prompt(sd)
        marker = _matches_scripted(prompt)
        if not marker:
            skipped += 1
            continue
        marked += 1
        compact_prompt = prompt[:60].replace("\n", " ")
        print(f"  {name[:8]}…  ← {marker!r}  prompt[:60]={compact_prompt!r}")
        if dry_run:
            continue
        env_cur["is_headless"] = True
        env_cur["_backfill_source"] = "prompt-pattern"
        env_cur["_backfill_marker"] = marker
        try:
            tmp = env_path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(env_cur, f, indent=2, ensure_ascii=False)
            os.replace(tmp, env_path)
        except OSError as e:
            print(f"    FAIL writing env.json: {e}")

    print()
    print(f"  scanned {scanned}; newly marked {marked}; already had signal {already}; no-match {skipped}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Flatten ~/.claude-audit/ layout (date-partitioned → flat)")
    ap.add_argument("--audit-dir", default=str(Path.home() / ".claude-audit"),
                    help="Audit dir (default: ~/.claude-audit)")
    ap.add_argument("--dry-run", action="store_true", help="Show plan without writing")
    ap.add_argument("--dedupe-flat", action="store_true",
                    help="Walk existing flat dirs and dedupe their audit logs (no migration)")
    ap.add_argument("--backfill-mode", action="store_true",
                    help="Classify historical sessions as scripted via prompt pattern match; "
                         "writes is_headless=true into env.json for matches. Never overrides "
                         "a live signal.")
    args = ap.parse_args()

    audit_dir = Path(args.audit_dir).expanduser().resolve()
    if not audit_dir.is_dir():
        print(f"audit dir does not exist: {audit_dir}", file=sys.stderr)
        return 2

    if args.dedupe_flat:
        print(f"dedupe-flat: scanning flat session dirs under {audit_dir}")
        if args.dry_run:
            print("(dry-run — no files will be written)")
        return dedupe_flat(audit_dir, args.dry_run)

    if args.backfill_mode:
        print(f"backfill-mode: inferring is_headless from prompt patterns under {audit_dir}")
        if args.dry_run:
            print("(dry-run — no files will be written)")
        return backfill_mode(audit_dir, args.dry_run)

    by_sid = discover(audit_dir)
    if not by_sid:
        print(f"no date-partitioned sessions found under {audit_dir}; nothing to migrate.")
        return 0

    print(f"migrate_flatten: {len(by_sid)} unique session(s) across "
          f"date-partitioned dirs in {audit_dir}")
    if args.dry_run:
        print("(dry-run — no files will be written)")
    print()

    multi_day = 0
    for sid, srcs in sorted(by_sid.items()):
        if len(srcs) > 1:
            multi_day += 1
        try:
            _, summary = consolidate_one(sid, srcs, audit_dir, args.dry_run)
        except Exception as e:
            print(f"  FAIL {sid[:8]}…  {type(e).__name__}: {e}")
            continue
        marker = "★" if len(srcs) > 1 else " "
        sources = ", ".join(s.parent.name for s in srcs)
        print(f"  {marker} {sid[:8]}…  ({sources}) → {summary}")

    print()
    print(f"  {multi_day} multi-day session(s) consolidated")
    if not args.dry_run:
        print()
        print("Original date-partitioned dirs were NOT removed. After verifying")
        print("the migration looks correct in the Web UI, run:")
        print(f"    rm -rf {audit_dir}/20*-*-*/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
