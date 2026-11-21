"""Timeline — the cross-source event brain.

THE GAP THIS CLOSES (the owner, 2026-08-14): "if we got an email from Alex first
with all details, then Alex messaged 'have you got the email', then he was
expecting delivery today — it should have immediately picked that task up."
Nothing in Sentinel correlated email + chat + calendar from the same person
about the same ask; each vertical saw its own silo, so the cross-source urgency
signal (the strongest signal there is) was invisible.

This module is that missing organ: every perceived event (Gmail, GChat DM,
GChat space, calendar, Cursor session, GitHub) lands in ONE SQLite timeline,
and `hot_threads()` ranks person-clusters by a deterministic urgency score:

    recency + urgency-keywords + repeat-pings + MULTI-SOURCE bonus + VIP bonus

The mission engine (verticals/missions.py) reads the top clusters and decides
what to actually go DO. Deterministic scoring here, judgment there — the LLM
never has to rediscover "Alex pinged twice across two channels about the same
thing" from raw noise.

Storage: ~/.sentinel/memory/timeline.db (WAL). Concurrency follows the
hard-won gateway.py rules: per-THREAD connections + one write lock (a shared
connection across threads SIGSEGV'd pre-v0.11 — see BRAINLIFT failure museum).
Ingest is idempotent: the event id is `source:source_id`, INSERT OR IGNORE.
All text is secret-scrubbed at the write boundary (same rule as gateway).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("timeline")

_DB_PATH = Path(os.getenv("SENTINEL_TIMELINE_DB",
                          str(Path.home() / ".sentinel" / "memory" / "timeline.db")))

_local = threading.local()
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,   -- source:source_id (natural dedup key)
    ts          TEXT NOT NULL,      -- ISO8601 UTC of the event itself
    source      TEXT NOT NULL,      -- gmail | gchat_dm | gchat_space | calendar | cursor | github
    sender      TEXT DEFAULT '',    -- display name ("Alex Rivera")
    sender_key  TEXT DEFAULT '',    -- stable identity (email addr / users/<id>)
    channel     TEXT DEFAULT '',    -- space name / 'inbox' / repo
    kind        TEXT DEFAULT 'message',
    text        TEXT DEFAULT '',    -- subject + snippet, scrubbed
    urgency     INTEGER DEFAULT 0,  -- deterministic 0-3 stamped at ingest
    meta        TEXT DEFAULT '{}',
    mission_id  TEXT DEFAULT '',    -- set once a mission was opened for it
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_sender ON events(sender_key);
"""

# Deterministic urgency vocabulary — stamped at ingest so scoring needs no LLM.
_URGENT_RE = re.compile(
    r"(?:\burgent\b|\basap\b|\btoday\b|\beod\b|\bdeadline\b|\breminder\b"
    r"|\bimmediate\b|\bblocking\b|\bblocked on\b|\bby tomorrow\b|\boverdue\b"
    r"|did you (?:get|see|receive)|have you (?:got|seen|done)|any update"
    r"|following up|follow.up|\?\?)", re.I)

_ASK_RE = re.compile(
    r"(?:\bcan you\b|\bcould you\b|\bplease\b|\bwe need\b|\bneed you\b"
    r"|\bset ?up\b|\bcreate\b|\bfix\b|\breview\b|\bsend\b|\bshare\b"
    r"|\bdeliver\b|\bdeploy\b|\bendpoint\b|\bby (?:mon|tue|wed|thu|fri|sat|sun|today|tomorrow))", re.I)


def _scrub(text: str) -> str:
    """Secret-scrub at the write boundary (reuse the gateway's rule when present)."""
    try:
        from memory.gateway import _scrub as gw_scrub
        return gw_scrub(text)
    except Exception:
        return text


def _conn() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is not None:
        return con
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    _local.con = con
    return con


def stamp_urgency(text: str) -> int:
    """0 = ambient, 1 = contains an ask, 2 = urgent language, 3 = both."""
    score = 0
    if _ASK_RE.search(text or ""):
        score += 1
    if _URGENT_RE.search(text or ""):
        score += 2
    return min(score, 3)


def ingest(events: list[dict]) -> int:
    """Idempotently add events. Each event dict needs at least
    {id | source_id, source, ts?, sender?, sender_key?, channel?, text?}.
    Returns the number of NEW rows."""
    if not events:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    with _write_lock:
        con = _conn()
        for ev in events:
            try:
                sid = str(ev.get("id") or ev.get("source_id") or "").strip()
                source = str(ev.get("source", "")).strip()
                if not sid or not source:
                    continue
                eid = sid if sid.startswith(f"{source}:") else f"{source}:{sid}"
                text = _scrub(str(ev.get("text", ""))[:2000])
                cur = con.execute(
                    "INSERT OR IGNORE INTO events "
                    "(id, ts, source, sender, sender_key, channel, kind, text, "
                    " urgency, meta, mission_id, ingested_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid,
                     str(ev.get("ts") or now),
                     source,
                     str(ev.get("sender", ""))[:200],
                     str(ev.get("sender_key", ""))[:200],
                     str(ev.get("channel", ""))[:200],
                     str(ev.get("kind", "message"))[:40],
                     text,
                     int(ev.get("urgency", stamp_urgency(text))),
                     json.dumps(ev.get("meta", {}), default=str)[:2000],
                     "",
                     now))
                new += cur.rowcount
            except Exception as e:
                logger.warning("timeline ingest skipped one event: %s", e)
        con.commit()
    return new


def recent(hours: float = 48, source: str = "", limit: int = 200) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q = "SELECT * FROM events WHERE ts >= ?"
    args: list = [since]
    if source:
        q += " AND source = ?"
        args.append(source)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in _conn().execute(q, args).fetchall()]


def get_event(event_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def update_text(event_id: str, text: str) -> None:
    """Enrich an event's text (e.g. subject-only email → full body) and
    re-stamp its deterministic urgency from the richer content."""
    text = _scrub(str(text)[:2000])
    with _write_lock:
        con = _conn()
        con.execute("UPDATE events SET text = ?, urgency = ? WHERE id = ?",
                    (text, stamp_urgency(text), event_id))
        con.commit()


def thread_view(sender_key: str, hours: float = 168, limit: int = 40) -> list[dict]:
    """Everything one person said/sent across ALL sources, oldest first —
    the 'Alex story so far' a mission brief is built from."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = _conn().execute(
        "SELECT * FROM events WHERE sender_key = ? AND ts >= ? ORDER BY ts ASC LIMIT ?",
        (sender_key, since, limit)).fetchall()
    return [dict(r) for r in rows]


def _vips() -> set[str]:
    try:
        from verticals.day import load_vips
        return {v.strip().lower() for v in (load_vips() or []) if v.strip()}
    except Exception:
        return set()


def _resolve_person(name: str) -> str:
    """Display name → canonical email via the people roster ('' if unknown)."""
    try:
        from tools import people
        hits = people.resolve_name(name, limit=1)
        if hits and hits[0].get("email"):
            return str(hits[0]["email"]).lower()
    except Exception:
        pass
    return ""


def _canonical_key(e: dict, cache: dict) -> str:
    """IDENTITY FUSION: the same human must be ONE cluster even though Gmail
    keys them by email and GChat by an opaque `users/<id>`. Without this,
    'Alex emailed, then Alex pinged on chat' looks like two lukewarm people
    instead of one escalating one — which is the strongest urgency signal."""
    key = (e.get("sender_key") or e.get("sender") or "").strip()
    if "@" in key:
        return key.lower()
    name = (e.get("sender") or "").strip()
    if not name:
        return key
    # gchat senders resolve to "Name <email>" — the email IS the canonical key
    m = re.search(r"<([^>]+@[^>]+)>", name)
    if m:
        return m.group(1).lower()
    if name not in cache:
        cache[name] = _resolve_person(name)
    return cache[name] or key


def hot_threads(hours: float = 48, min_score: float = 2.0, limit: int = 8) -> list[dict]:
    """Rank person-clusters by composite urgency. Excludes events already
    attached to a mission and self-sent noise.

    Score per cluster:
      + max event urgency (0-3)
      + 1.5 if the person pinged in ≥2 DIFFERENT sources (email + chat = the
        'did you get my email' escalation — the single strongest signal)
      + 0.5 per extra message beyond the first (capped +2)
      + 1.0 if VIP
      + 1.0 if the newest event is <6h old
    """
    events = [e for e in recent(hours=hours, limit=400) if not e.get("mission_id")]
    clusters: dict[str, list[dict]] = {}
    resolve_cache: dict[str, str] = {}
    for e in events:
        if e.get("kind") in ("self", "bot"):
            continue
        key = _canonical_key(e, resolve_cache)
        if not key:
            continue
        clusters.setdefault(key, []).append(e)

    vips = _vips()
    now = datetime.now(timezone.utc)
    ranked = []
    for key, evs in clusters.items():
        evs.sort(key=lambda e: e["ts"])
        sources = {e["source"] for e in evs}
        score = float(max(e["urgency"] for e in evs))
        if len(sources) >= 2:
            score += 1.5
        score += min(0.5 * (len(evs) - 1), 2.0)
        sender_lc = (evs[-1].get("sender") or key).lower()
        if any(v in sender_lc or sender_lc in v for v in vips):
            score += 1.0
        try:
            newest = datetime.fromisoformat(evs[-1]["ts"].replace("Z", "+00:00"))
            if newest.tzinfo is None:
                newest = newest.replace(tzinfo=timezone.utc)
            if (now - newest) < timedelta(hours=6):
                score += 1.0
        except ValueError:
            pass
        if score >= min_score:
            ranked.append({"sender_key": key,
                           "sender": evs[-1].get("sender") or key,
                           "score": round(score, 2),
                           "sources": sorted(sources),
                           "events": evs})
    ranked.sort(key=lambda c: c["score"], reverse=True)
    return ranked[:limit]


def mark_mission(event_ids: list[str], mission_id: str) -> None:
    if not event_ids:
        return
    with _write_lock:
        con = _conn()
        con.executemany("UPDATE events SET mission_id = ? WHERE id = ?",
                        [(mission_id, eid) for eid in event_ids])
        con.commit()


def render_cluster(cluster: dict, max_chars: int = 3500) -> str:
    """A cluster as the chronological story a mission brief embeds."""
    lines = [f"PERSON: {cluster.get('sender','?')}  (score {cluster.get('score')}, "
             f"sources: {', '.join(cluster.get('sources', []))})"]
    for e in cluster.get("events", []):
        ts = (e.get("ts") or "")[:16].replace("T", " ")
        lines.append(f"[{ts} · {e['source']}{' · ' + e['channel'] if e.get('channel') else ''}] "
                     f"{e.get('text','')[:600]}")
    out = "\n".join(lines)
    return out[:max_chars]
