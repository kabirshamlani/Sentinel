"""Read-only bridges: pull OpenClaw's and Hermes's memory INTO the canonical
gateway, so there is ONE place to query "what the system knows."

Research verdict (agent-army-research/13): Hermes/OpenClaw stores are loop-
coupled and single-writer — safe to READ, unsafe to make the shared hub. So the
Conductor's gateway stays canonical; these bridges tail the external stores in
read-only mode and mirror salient rows in. We never write back into them
concurrently (avoids SQLite lock races). Idempotent via dedupe.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from memory import gateway

logger = logging.getLogger("mem-sync")

OPENCLAW_DB = Path.home() / ".openclaw/memory/main.sqlite"
HERMES_DB = Path.home() / ".hermes/state.db"   # present only if Hermes installed


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        return con
    except Exception as e:
        logger.debug("cannot open %s ro: %s", path, e)
        return None


async def sync_openclaw(limit: int = 200) -> int:
    """Mirror OpenClaw memory chunks (its `chunks` table: text + source) in."""
    con = _ro(OPENCLAW_DB)
    if not con:
        return 0
    n = 0
    try:
        rows = con.execute(
            "SELECT text, source FROM chunks WHERE text IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        for r in rows:
            txt = (r["text"] or "").strip()
            if len(txt) < 20:
                continue
            gateway.remember(txt[:800], kind="note", source=f"openclaw:{r['source'] or 'mem'}",
                             confidence=0.5, meta={"external": "openclaw"})
            n += 1
    except sqlite3.OperationalError as e:
        logger.debug("openclaw sync query failed: %s", e)
    finally:
        con.close()
    if n:
        logger.info("synced %d OpenClaw memory chunks into gateway", n)
    return n


async def sync_hermes(limit: int = 200) -> int:
    """Mirror Hermes state.db salient rows in (only if Hermes is installed +
    enabled). Best-effort schema probing (Hermes tables vary by version)."""
    try:
        from brain import hermes_adapter
        if not hermes_adapter.available():
            return 0
    except Exception:
        return 0
    con = _ro(HERMES_DB)
    if not con:
        return 0
    n = 0
    try:
        tables = [r["name"] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        # look for a table with a text-ish column (memories/messages/facts)
        for t in ("memories", "facts", "messages", "sessions"):
            if t not in tables:
                continue
            cols = [c["name"] for c in con.execute(f"PRAGMA table_info({t})").fetchall()]
            textcol = next((c for c in ("content", "text", "body", "summary") if c in cols), None)
            if not textcol:
                continue
            for r in con.execute(f"SELECT {textcol} AS t FROM {t} "
                                 f"WHERE {textcol} IS NOT NULL LIMIT ?", (limit,)).fetchall():
                txt = (r["t"] or "").strip()
                # skip our own echoed prompts / trivial turns (noise, not knowledge)
                if len(txt) < 20 or txt.lower().startswith((
                        "reply with", "from your memory", "draft a concise",
                        "output only", "return only")):
                    continue
                gateway.remember(txt[:800], kind="note", source="hermes",
                                 confidence=0.5, meta={"external": "hermes"})
                n += 1
            break
    except sqlite3.OperationalError as e:
        logger.debug("hermes sync query failed: %s", e)
    finally:
        con.close()
    if n:
        logger.info("synced %d Hermes rows into gateway", n)
    return n


async def sync_all() -> dict:
    return {"openclaw": await sync_openclaw(), "hermes": await sync_hermes()}
