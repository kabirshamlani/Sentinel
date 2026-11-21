"""Unified memory gateway — ONE store, one source of truth.

The owner's requirement: "it shouldn't be like some memory stored here, some there —
it will create a complete mess." Before this, knowledge lived in 6+ files
(semantic_facts.jsonl, reasoning_bank.jsonl, skillbook.json, error_journal.jsonl,
MEMORY.md, BRAINLIFT.md) plus OpenClaw's and Hermes's own stores. This module is
the single canonical store everything goes through.

Design:
  * One SQLite DB (~/.sentinel/memory/unified.db) with FTS5 full-text search —
    stdlib only, no vector DB, sub-ms recall, survives for years.
  * ONE table `memory` with a `kind` discriminator: fact | lesson | skill |
    correction | error | preference | profile | project | world | note.
    Every row: id, kind, text, meta(json), source, confidence, provenance,
    created, last_used, uses, status(active|archived), embedding-free.
  * Typed helpers so callers don't touch SQL. semantic.py + reasoning_bank.py
    become thin facades over this (existing callers keep working).
  * Corrections are sacred (never decay). Consolidation curates in place.
  * External stores (OpenClaw main.sqlite, Hermes state.db) SYNC INTO this via
    memory/sync.py — the gateway stays the canonical "what the system knows."

Concurrency: SQLite WAL + a short busy_timeout handles the daemon's async tasks
writing concurrently. One process, many coroutines — fine.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from config.paths import MEMORY_ROOT

DB_PATH = MEMORY_ROOT / "unified.db"

KINDS = {"fact", "lesson", "skill", "correction", "error", "preference",
         "profile", "project", "world", "note"}
SACRED = {"correction"}          # never decays
_PRIORITY = {"correction": 0, "preference": 1, "profile": 2, "skill": 3,
             "lesson": 4, "project": 5, "fact": 6, "error": 7, "world": 8, "note": 9}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_local = threading.local()
_write_lock = threading.Lock()   # serialize writes (SELECT-then-INSERT) across threads
_init_done = False


def _db() -> sqlite3.Connection:
    """Per-THREAD connection. A single sqlite3.Connection shared across OS
    threads SIGSEGVs under concurrent execute() (check_same_thread=False only
    silences the check, it doesn't make it safe). The daemon mostly uses asyncio
    (one thread), but some paths run in thread-pool executors (memory/sync,
    _run_async), so we give each thread its own connection. WAL lets them read
    concurrently; the write lock serializes the read-modify-write in remember()."""
    global _init_done
    conn = getattr(_local, "conn", None)
    if conn is None:
        MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        if not _init_done:
            with _write_lock:            # guard first-run schema init against a race
                if not _init_done:
                    _init(conn)
                    _init_done = True
        _local.conn = conn
    return conn


def _init(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS memory (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        text        TEXT NOT NULL,
        meta        TEXT DEFAULT '{}',
        source      TEXT DEFAULT '',
        confidence  REAL DEFAULT 0.8,
        created     TEXT NOT NULL,
        last_used   TEXT NOT NULL,
        uses        INTEGER DEFAULT 0,
        status      TEXT DEFAULT 'active'
    );
    CREATE INDEX IF NOT EXISTS idx_kind   ON memory(kind, status);
    CREATE INDEX IF NOT EXISTS idx_status ON memory(status, last_used);
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
        USING fts5(text, id UNINDEXED, kind UNINDEXED);
    """)
    con.commit()
    _ensure_fts_healthy(con)


def _ensure_fts_healthy(con: sqlite3.Connection) -> None:
    """Rebuild the FTS index if it's the old contentless schema or out of sync
    with the memory table (self-healing — no manual migration needed)."""
    try:
        mem = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        fts = con.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        # sanity: MATCH must actually return a known row
        probe_ok = True
        if mem:
            row = con.execute("SELECT id, text FROM memory LIMIT 1").fetchone()
            tok = next((w for w in "".join(c if c.isalnum() else " " for c in row["text"]).split()
                        if len(w) > 3), None)
            if tok:
                hit = con.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_fts MATCH ?",
                                  (tok,)).fetchone()[0]
                probe_ok = hit > 0
        if fts != mem or not probe_ok:
            con.execute("DROP TABLE IF EXISTS memory_fts")
            con.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(text, id UNINDEXED, kind UNINDEXED)")
            for r in con.execute("SELECT id, kind, text FROM memory").fetchall():
                con.execute("INSERT INTO memory_fts(text, id, kind) VALUES (?,?,?)",
                            (r["text"], r["id"], r["kind"]))
            con.commit()
    except sqlite3.OperationalError:
        pass


def _fts_add(con, mid: str, kind: str, text: str) -> None:
    # standalone FTS5 table (own storage) — let SQLite assign the rowid so there
    # are never collisions, and MATCH works on the indexed `text` column.
    con.execute("INSERT INTO memory_fts(text, id, kind) VALUES (?, ?, ?)",
                (text, mid, kind))


# ------------------------------------------------------------------ writes

def _similar(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


import re as _re

# Redact secret-shaped substrings at the WRITE boundary, so nothing sensitive
# ever enters the store — and therefore can never be injected into an agent
# prompt (memory now reaches every agent) or synced out. error_journal captures
# raw error text and sync mirrors external notes, so this is the safety net.
_SECRET_RE = _re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
    r"AIza[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"  # JWT
    r"-----BEGIN [A-Z ]+PRIVATE KEY-----|AKIA[0-9A-Z]{16}|"            # PEM / AWS
    r"(?:api[_-]?key|token|password|secret|bearer)\s*[:=]\s*\S{8,})",
    _re.IGNORECASE)


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text or "")


def remember(text: str, kind: str = "note", *, source: str = "system",
             confidence: float = 0.8, meta: dict | None = None,
             dedupe: bool = True) -> dict:
    """Store (or merge) a memory. Returns the row. Dedupe merges near-identical
    same-kind rows (raising confidence + recency) instead of piling up.
    Secret-shaped text is redacted at this boundary (never persisted)."""
    text = _scrub((text or "").strip())
    if not text:
        return {}
    kind = kind if kind in KINDS else "note"
    con = _db()
    with _write_lock:                       # serialize read-modify-write across threads
        if dedupe:
            for r in con.execute("SELECT id, text, confidence, uses FROM memory "
                                 "WHERE kind=? AND status='active'", (kind,)).fetchall():
                if _similar(r["text"], text) > 0.7:
                    con.execute("UPDATE memory SET text=?, confidence=?, last_used=?, uses=uses+1 "
                                "WHERE id=?",
                                (text, min(1.0, max(r["confidence"], confidence) + 0.05),
                                 _now(), r["id"]))
                    con.execute("UPDATE memory_fts SET text=? WHERE id=?", (text, r["id"]))
                    con.commit()
                    return _row(con.execute("SELECT * FROM memory WHERE id=?", (r["id"],)).fetchone())
        mid = f"m-{uuid.uuid4().hex[:10]}"
        now = _now()
        # scrub string values in meta too (defense in depth — callers put only
        # structured fields there today, but never trust it as an unscrubbed channel)
        clean_meta = {k: (_scrub(v) if isinstance(v, str) else v)
                      for k, v in (meta or {}).items()}
        con.execute("INSERT INTO memory(id,kind,text,meta,source,confidence,created,last_used,uses,status)"
                    " VALUES (?,?,?,?,?,?,?,?,0,'active')",
                    (mid, kind, text, json.dumps(clean_meta), source, confidence, now, now))
        _fts_add(con, mid, kind, text)
        con.commit()
        return _row(con.execute("SELECT * FROM memory WHERE id=?", (mid,)).fetchone())


def touch(ids: list[str]) -> None:
    if not ids:
        return
    con = _db()
    with _write_lock:
        con.executemany("UPDATE memory SET last_used=?, uses=uses+1 WHERE id=?",
                        [(_now(), i) for i in ids])
        con.commit()


def archive(mid: str) -> bool:
    con = _db()
    with _write_lock:
        cur = con.execute("UPDATE memory SET status='archived' WHERE id=? AND status='active'", (mid,))
        con.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------- reads

def _row(r) -> dict:
    d = dict(r)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except json.JSONDecodeError:
        d["meta"] = {}
    return d


def all_of(kind: str | None = None, limit: int = 1000) -> list[dict]:
    con = _db()
    if kind:
        rows = con.execute("SELECT * FROM memory WHERE kind=? AND status='active' "
                           "ORDER BY last_used DESC LIMIT ?", (kind, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM memory WHERE status='active' "
                           "ORDER BY last_used DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def search(query: str, kinds: list[str] | None = None, k: int = 8) -> list[dict]:
    """FTS5 full-text recall. Falls back to token-overlap if FTS misses."""
    con = _db()
    q = " OR ".join(w for w in "".join(c if c.isalnum() else " " for c in query).split() if len(w) > 2)
    hits: list[dict] = []
    if q:
        try:
            rows = con.execute(
                "SELECT m.* FROM memory_fts f JOIN memory m ON m.id=f.id "
                "WHERE memory_fts MATCH ? AND m.status='active' LIMIT ?", (q, k * 3)).fetchall()
            hits = [_row(r) for r in rows]
        except sqlite3.OperationalError:
            hits = []
    if not hits:                       # fallback: fuzzy token overlap over active rows
        import re as _re
        def _toks(s):
            return {w for w in _re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}
        def _fuzzy_overlap(a: set, b: set) -> int:
            # exact + prefix match (paginate~pagination, issue~issues) via >=4-char stems
            score = len(a & b)
            for x in a - b:
                for y in b - a:
                    if len(x) >= 4 and len(y) >= 4 and (x.startswith(y[:4]) or y.startswith(x[:4])):
                        score += 1
                        break
            return score
        qt = _toks(query)
        scored = []
        for r in all_of(limit=500):
            ov = _fuzzy_overlap(qt, _toks(r["text"]))
            if ov:
                scored.append((ov, r))
        hits = [r for _, r in sorted(scored, key=lambda x: -x[0])]
    if kinds:
        hits = [h for h in hits if h["kind"] in kinds]
    hits.sort(key=lambda h: (_PRIORITY.get(h["kind"], 9), -h.get("confidence", 0)))
    return hits[:k]


def prompt_block(context_hint: str = "", budget_chars: int = 2000) -> str:
    """Everything the system knows, relevant-first, corrections always included.
    Replaces both semantic.prompt_block and reasoning_bank.retrieve for prompts."""
    con = _db()
    # Always-relevant standing knowledge (regardless of the query): corrections
    # are sacred, and preferences/profile define who the owner is — an agent should
    # never act without them just because the query didn't lexically match.
    # Cap the always-include set so this block (injected into EVERY agent now)
    # can't grow unbounded as corrections accumulate over months. Most-recent
    # corrections win; older ones are still in the store, just not force-injected.
    always = (all_of("correction", 12) + all_of("preference", 10) + all_of("profile", 6))
    hits = search(context_hint, k=14) if context_hint else all_of(limit=20)
    # Defense in depth: search-hit rows of the LEAST-trusted kinds (note/error/
    # world — where external OpenClaw/Hermes content + raw error text land) are
    # NOT force-injected into agent prompts. They're already secret-scrubbed at
    # write, but keeping them out of the prompt is a second wall. Lessons/skills
    # (curated, Sentinel-authored) and the always-set are fine.
    _UNTRUSTED_INJECT = {"note", "error", "world"}
    hits = [h for h in hits if h["kind"] not in _UNTRUSTED_INJECT]
    seen, picked = set(), []
    for r in always + hits:             # standing knowledge first, then query-relevant
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        picked.append(r)
    lines, used, ids = ["What the system knows (corrections are absolute):"], 0, []
    for r in picked:
        tag = r["kind"].upper()
        line = f"- [{tag}] {r['text']}"
        if used + len(line) > budget_chars and r["kind"] not in SACRED:
            continue
        lines.append(line)
        used += len(line)
        ids.append(r["id"])
    touch(ids)
    return "\n".join(lines) if len(lines) > 1 else ""


# ------------------------------------------------------------- maintenance

def consolidate(max_active: int = 400, decay_days: int = 90) -> dict:
    """Deterministic curation: decay unused (non-sacred) rows to archive, cap the
    active set. Called nightly. No LLM, predictable."""
    from datetime import timedelta
    con = _db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=decay_days)).isoformat()
    with _write_lock:                       # serialize the long curation transaction
        dec = con.execute("UPDATE memory SET status='archived' WHERE status='active' "
                          "AND kind NOT IN ('correction','preference','profile') "
                          "AND last_used < ? AND uses < 2", (cutoff,)).rowcount
        active = con.execute("SELECT COUNT(*) c FROM memory WHERE status='active'").fetchone()["c"]
        capped = 0
        if active > max_active:
            # archive the weakest surplus (never corrections)
            surplus = active - max_active
            rows = con.execute(
                "SELECT id FROM memory WHERE status='active' AND kind NOT IN "
                "('correction','preference','profile') ORDER BY uses ASC, last_used ASC "
                "LIMIT ?", (surplus,)).fetchall()
            for r in rows:
                con.execute("UPDATE memory SET status='archived' WHERE id=?", (r["id"],))
                capped += 1
        con.commit()
    return {"decayed": dec, "capped": capped,
            "active": con.execute("SELECT COUNT(*) c FROM memory WHERE status='active'").fetchone()["c"]}


def stats() -> dict:
    con = _db()
    rows = con.execute("SELECT kind, COUNT(*) c FROM memory WHERE status='active' GROUP BY kind").fetchall()
    by_kind = {r["kind"]: r["c"] for r in rows}
    arch = con.execute("SELECT COUNT(*) c FROM memory WHERE status='archived'").fetchone()["c"]
    return {"active": sum(by_kind.values()), "by_kind": by_kind, "archived": arch,
            "db": str(DB_PATH)}
