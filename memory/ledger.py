"""Action ledger — the evidence-gated record of everything Sentinel does for the owner.

The v0.16 blueprint invariant (docs 16+17, adversarially agreed): a task may transition
to `done` ONLY on a Python-captured evidence token from the trusted executor (a Gmail
message-id, a git SHA + PR URL, a calendar event-id, the count+ids of what was read) —
NEVER on the model saying "done". This flat append-only ledger is where that lives.

Two jobs:
  1. Reliability / getting-the owner-out-of-the-loop: when Sentinel says "sent", it provably
     sent — the ledger row carries the real evidence, so he can stop double-checking.
  2. Pro memory: it IS the durable "what did I do last week / 2 months ago" record —
     query by time/person/status and answer from real logged actions, not vibes.

Flat, not a DAG (a personal assistant's tasks are 1-3 steps; parent_id captures the
chain when needed). Own table in the one unified SQLite DB; reuses gateway._db().
Secret-scrubbed at the write boundary like everything else.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from memory.gateway import _db, _scrub

logger = logging.getLogger("ledger")

_TABLE = "action_ledger"

# lifecycle: open -> (attempted) -> done | failed | proposed(awaiting approval)
STATUSES = ("open", "attempted", "proposed", "done", "failed")


def _ensure(con: sqlite3.Connection) -> None:
    con.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            id            TEXT PRIMARY KEY,
            request_text  TEXT,          -- what the owner asked (or the autonomous trigger)
            tool          TEXT,          -- the action taken (send_email, read_meeting, run_command…)
            target        TEXT,          -- concrete object (recipient, repo, path) for audit
            status        TEXT DEFAULT 'open',
            evidence      TEXT DEFAULT '',   -- Python-captured token; REQUIRED to reach 'done'
            detail        TEXT DEFAULT '{{}}',
            parent_id     TEXT DEFAULT '',   -- chains multi-step work (flat, resolvable)
            channel       TEXT DEFAULT '',
            created       REAL,
            updated       REAL
        )""")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_ledger_created ON {_TABLE}(created)")
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_ledger_status ON {_TABLE}(status)")
    con.commit()


def open_action(request_text: str, tool: str, *, target: str = "", detail: dict | None = None,
                parent_id: str = "", channel: str = "") -> str:
    """Record the START of an action. Returns the ledger id to close later."""
    con = _db(); _ensure(con)
    aid = "act-" + uuid.uuid4().hex[:12]
    now = time.time()
    con.execute(
        f"INSERT INTO {_TABLE}(id,request_text,tool,target,status,detail,parent_id,channel,created,updated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, _scrub(request_text or "")[:500], tool, _scrub(target or "")[:200], "open",
         json.dumps(detail or {})[:2000], parent_id, channel, now, now))
    con.commit()
    return aid


def close_done(aid: str, evidence: str) -> bool:
    """Mark an action done — ONLY valid with a concrete evidence token. An empty
    evidence string is refused (the whole point of the invariant: no evidence, no done)."""
    ev = (evidence or "").strip()
    if not ev:
        logger.warning("ledger: refusing to close %s as done with no evidence", aid)
        return mark(aid, "attempted", evidence="")
    return mark(aid, "done", evidence=ev)


def mark(aid: str, status: str, *, evidence: str = "") -> bool:
    if status not in STATUSES:
        return False
    con = _db(); _ensure(con)
    con.execute(f"UPDATE {_TABLE} SET status=?, evidence=COALESCE(NULLIF(?,''), evidence), updated=? "
                "WHERE id=?", (status, _scrub(evidence or "")[:300], time.time(), aid))
    con.commit()
    return True


def record(request_text: str, tool: str, *, target: str = "", evidence: str = "",
           status: str = "", detail: dict | None = None, channel: str = "") -> str:
    """One-shot convenience: open + immediately close with (or without) evidence.
    Status defaults to 'done' if evidence is present, else 'attempted'."""
    aid = open_action(request_text, tool, target=target, detail=detail, channel=channel)
    if status:
        mark(aid, status, evidence=evidence)
    elif evidence:
        close_done(aid, evidence)
    else:
        mark(aid, "attempted")
    return aid


# ─────────────────────────── recall (pro memory) ───────────────────────────

def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "request": r["request_text"], "tool": r["tool"],
            "target": r["target"], "status": r["status"], "evidence": r["evidence"],
            "when": datetime.fromtimestamp(r["created"], timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "channel": r["channel"]}


def recent(days: float = 7, limit: int = 50, status: str = "") -> list[dict]:
    """Actions in the last N days (optionally filtered by status) — newest first."""
    con = _db(); _ensure(con)
    since = time.time() - days * 86400
    q = f"SELECT * FROM {_TABLE} WHERE created >= ?"
    args: list = [since]
    if status:
        q += " AND status=?"; args.append(status)
    q += " ORDER BY created DESC LIMIT ?"; args.append(limit)
    return [_row(r) for r in con.execute(q, args).fetchall()]


def search(query: str, days: float = 120, limit: int = 40) -> list[dict]:
    """Free-text-ish recall over the ledger for 'what did I do about X / N months ago'.
    LIKE over request/tool/target (the ledger is small; no FTS needed). Newest first."""
    con = _db(); _ensure(con)
    since = time.time() - days * 86400
    like = f"%{(query or '').strip()}%"
    rows = con.execute(
        f"SELECT * FROM {_TABLE} WHERE created >= ? AND "
        "(request_text LIKE ? OR tool LIKE ? OR target LIKE ?) ORDER BY created DESC LIMIT ?",
        (since, like, like, like, limit)).fetchall()
    return [_row(r) for r in rows]


def summary_line() -> str:
    """One-line health for the heartbeat/reporter: counts by status in last 24h."""
    con = _db(); _ensure(con)
    since = time.time() - 86400
    rows = con.execute(
        f"SELECT status, COUNT(*) c FROM {_TABLE} WHERE created>=? GROUP BY status", (since,)
    ).fetchall()
    if not rows:
        return "no logged actions in 24h"
    return " · ".join(f"{r['c']} {r['status']}" for r in rows)
