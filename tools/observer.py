"""Ambient observer — figures out what the owner is working on, privacy-safe.

Passive signals, zero LLM cost (Embra pattern from research doc 10):
  * recent file changes (Spotlight mdfind on watched roots)
  * Cursor chat sessions (state.vscdb — composerHeaders + cursorDiskKV, read-only)
  * Claude Code sessions (~/.claude/projects/*/*.jsonl)
  * next calendar events (gog)

Output is an ABSTRACT work model (topics/projects/intents — never raw content
dumps) stored at STATE_DIR/work_context.json and rendered compactly for
prompts. This is what lets Sentinel connect the dots and act proactively.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conductor import kv

logger = logging.getLogger("observer")

CURSOR_DB = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
CURSOR_WS = Path.home() / "Library/Application Support/Cursor/User/workspaceStorage"
CLAUDE_PROJECTS = Path.home() / ".claude/projects"
_EXCLUDE = ("/.git/", "node_modules", "/Library/", "/.venv/", "__pycache__",
            ".sentinel", "DerivedData", "/.Trash/")


def _watch_roots() -> list[str]:
    try:
        from conductor.loop import load_config
        roots = load_config().get("watch_roots")
        if roots:
            return [os.path.expanduser(r) for r in roots]
    except Exception:
        pass
    return [str(Path.home() / "Projects")]


# ---------- recent files (mdfind) ----------

async def recent_files(hours: float = 6) -> list[dict]:
    out: list[dict] = []
    secs = int(hours * 3600)
    for root in _watch_roots():
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind", "-onlyin", root, f"kMDItemFSContentChangeDate >= $time.now(-{secs})",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            for line in raw.decode(errors="ignore").splitlines():
                p = line.strip()
                if p and os.path.isfile(p) and not any(x in p for x in _EXCLUDE):
                    out.append({"path": p, "project": _project_of(p, root)})
        except Exception as e:
            logger.debug("mdfind failed for %s: %s", root, e)
    return out[:60]


def _project_of(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return rel.split(os.sep)[0] if os.sep in rel else rel


# ---------- Cursor sessions (verified 2026 schema) ----------

def _cursor_workspace_folder(workspace_id: str) -> str:
    try:
        ws = json.loads((CURSOR_WS / workspace_id / "workspace.json").read_text())
        return ws.get("folder", "").replace("file://", "")
    except Exception:
        return ""


def cursor_sessions(hours: float = 12, with_asks: bool = True) -> list[dict]:
    """Recent Cursor agent sessions + (for the freshest) the last user asks."""
    if not CURSOR_DB.exists():
        return []
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp() * 1000)
    rows = []
    try:
        con = sqlite3.connect(f"file:{CURSOR_DB}?mode=ro", uri=True, timeout=3)
        cur = con.execute(
            "SELECT composerId, workspaceId, lastUpdatedAt, value FROM composerHeaders "
            "WHERE isSubagent=0 AND lastUpdatedAt >= ? ORDER BY recency DESC LIMIT 8", (cutoff_ms,))
        for cid, wsid, updated, value in cur.fetchall():
            try:
                v = json.loads(value or "{}")
            except json.JSONDecodeError:
                v = {}
            rows.append({
                "composer_id": cid,
                "name": (v.get("name") or "")[:80],
                "subtitle": (v.get("subtitle") or "")[:100],
                "files_changed": v.get("filesChangedCount", 0),
                "folder": _cursor_workspace_folder(wsid or ""),
                "last_active": datetime.fromtimestamp(updated / 1000, tz=timezone.utc).isoformat(),
            })
        if with_asks and rows:
            for r in rows[:2]:
                cur = con.execute(
                    "SELECT json_extract(value,'$.text') FROM cursorDiskKV "
                    "WHERE key LIKE ? AND json_extract(value,'$.type')=1 "
                    "AND json_extract(value,'$.text')!='' "
                    "ORDER BY json_extract(value,'$.createdAt') DESC LIMIT 3",
                    (f"bubbleId:{r['composer_id']}:%",))
                r["recent_asks"] = [t[0][:160] for t in cur.fetchall() if t and t[0]]
        con.close()
    except Exception as e:
        logger.debug("cursor db read failed: %s", e)
    return rows


# ---------- Claude Code sessions ----------

def claude_sessions(hours: float = 12) -> list[dict]:
    if not CLAUDE_PROJECTS.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    hits = []
    try:
        for proj in CLAUDE_PROJECTS.iterdir():
            if not proj.is_dir():
                continue
            for f in proj.glob("*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    continue
                hits.append((f.stat().st_mtime, f))
    except Exception:
        return []
    out = []
    for mtime, f in sorted(hits, reverse=True)[:5]:
        info = {"last_active": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()}
        try:  # read tail cheaply for cwd/branch/last user ask
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 200_000))
                tail = fh.read().decode(errors="ignore").splitlines()
            for line in reversed(tail):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not info.get("cwd") and rec.get("cwd"):
                    info["cwd"] = rec["cwd"]
                    info["branch"] = rec.get("gitBranch", "")
                if not info.get("last_ask") and rec.get("type") == "user" and not rec.get("isSidechain"):
                    content = rec.get("message", {}).get("content", "")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if isinstance(content, str) and content.strip() and not content.startswith(("<", "Caveat")):
                        info["last_ask"] = content.strip()[:160]
                if info.get("cwd") and info.get("last_ask"):
                    break
        except Exception:
            pass
        if info.get("cwd"):
            out.append(info)
    return out


# ---------- the work model ----------

async def build_work_context(hours: float = 8) -> dict:
    files = await recent_files(hours)
    by_project: dict[str, int] = {}
    for f in files:
        by_project[f["project"]] = by_project.get(f["project"], 0) + 1
    calendar = []
    try:
        from tools import gog_workspace as gw
        if gw.available():
            calendar = (await gw.upcoming_events(6))[:6]
    except Exception:
        pass
    ctx = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_projects": sorted(by_project.items(), key=lambda kv_: -kv_[1])[:6],
        # THREAD: the 79GB Cursor DB can take minutes on the bubbleId queries —
        # synchronous here it froze the daemon loop (same bug hit missions'
        # perceive_cursor, found live 2026-08-14).
        "cursor": await asyncio.to_thread(cursor_sessions, hours),
        "claude_code": claude_sessions(hours),
        "calendar": [{"summary": (e.get("summary") or "")[:70],
                      "start": str(e.get("start", ""))[:19]} for e in calendar],
    }
    kv.put("work_context", ctx)
    return ctx


def summary_text(ctx: dict | None = None, max_chars: int = 1200) -> str:
    """Compact prompt block: what the owner is working on right now."""
    ctx = ctx or kv.get("work_context") or {}
    if not ctx:
        return ""
    lines = ["What the owner is working on (ambient signals):"]
    if ctx.get("active_projects"):
        lines.append("- Files changing in: " +
                     ", ".join(f"{p} ({n})" for p, n in ctx["active_projects"][:4]))
    for c in (ctx.get("cursor") or [])[:2]:
        ask = f' — asked: "{c["recent_asks"][0]}"' if c.get("recent_asks") else ""
        lines.append(f"- Cursor: {c.get('name') or 'session'} in {os.path.basename(c.get('folder') or '')}{ask}")
    for s in (ctx.get("claude_code") or [])[:2]:
        lines.append(f"- Claude Code: {os.path.basename(s.get('cwd',''))} [{s.get('branch','')}]"
                     + (f' — "{s["last_ask"]}"' if s.get("last_ask") else ""))
    for e in (ctx.get("calendar") or [])[:3]:
        lines.append(f"- Calendar: {e['summary']} at {e['start']}")
    return "\n".join(lines)[:max_chars]
