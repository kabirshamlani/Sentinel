"""People resolver — the identity layer so the owner can refer to anyone by name.

The problem: Google Chat 1:1 DM spaces expose only an opaque `users/<id>` and
carry NO display name, so DM summaries showed raw `spaces/AAAA` ids and there
was no way to act on "reply to Jordan". Named rooms already resolve fine.

The fix: harvest a bidirectional roster from the sources that DO carry names —
Gmail `From:` headers (`Name <email>`), Calendar attendees (emails), and the
`firstname.lastname@` email convention — cache it in the unified SQLite DB, and
expose lookups both directions:

    name  -> email/space   ("message Jordan"  -> jordan.park@example.com -> DM space)
    email -> name          (summary shows "Jordan Park", not spaces/AAAA)
    id    -> name           (Chat users/<id> resolved via correlated email)

This is the "compiled entity layer" from the v0.16 blueprint (Theme 6): one
durable roster the whole system reads, refreshed in the background, never
re-derived per turn. All reads are Tier-R (no mutation); building the roster
only reads Gmail/Calendar which the owner already authorizes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from difflib import SequenceMatcher

from config.paths import ensure_home
from memory.gateway import _db  # reuse the one unified connection factory

logger = logging.getLogger("people")

# a person row lives in its own table in the unified DB — kept out of the
# generic `memory` FTS so it never pollutes the prompt-block or fuzzy recall.
_TABLE = "people"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# "Name <email>"  or  "email"  or  '"Name" <email>'
_FROM_RE = re.compile(r'^\s*"?([^"<]*?)"?\s*<?([\w.+-]+@[\w.-]+\.\w+)>?\s*$')

# roster freshness: rebuild if the cache is older than this (seconds)
_STALE_AFTER = 6 * 3600


# ─────────────────────────── schema ───────────────────────────

def _ensure(con: sqlite3.Connection) -> None:
    con.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
            email        TEXT PRIMARY KEY,
            name         TEXT,
            chat_user_id TEXT,          -- Google Chat users/<id>, once correlated
            dm_space     TEXT,          -- spaces/<id> for the 1:1 DM, once known
            source       TEXT,          -- gmail | calendar | pattern | manual
            seen         INTEGER,       -- interaction count (recency/salience)
            updated      REAL
        )"""
    )
    con.execute(f"CREATE INDEX IF NOT EXISTS idx_people_name ON {_TABLE}(name)")
    con.commit()


# ───────────────────────── name helpers ───────────────────────

def _name_from_email(email: str) -> str:
    """Derive 'Firstname Lastname' from a `firstname.lastname@` local part."""
    local = email.split("@", 1)[0]
    # strip +tags and digits, split on . _ -
    local = re.sub(r"\+.*$", "", local)
    parts = [p for p in re.split(r"[._-]+", local) if p and not p.isdigit()]
    if not parts:
        return ""
    return " ".join(p.capitalize() for p in parts)


def _clean_name(raw: str, email: str) -> str:
    """Prefer a real display name; fall back to the email-derived one."""
    raw = (raw or "").strip().strip('"').strip()
    # strip bot/relay aliases: "Stan Huseletov via Read AI" -> "Stan Huseletov"
    raw = re.split(r"\s+via\s+", raw, maxsplit=1)[0].strip()
    # reject noise display names ("noreply", the email echoed as the name, etc.)
    if raw and "@" not in raw and raw.lower() not in ("", "me", "unknown"):
        return raw
    return _name_from_email(email)


def _is_noise_email(email: str) -> bool:
    e = email.lower()
    return any(h in e for h in (
        "noreply", "no-reply", "donotreply", "do-not-reply", "notifications@",
        "notification@", "mailer-daemon", "bounce", "@google.com",
        "@resource.calendar", "calendar-notification",
    ))


def _is_bot_email(email: str) -> bool:
    """Automated/relay senders (Read AI, Otter, executiveassistant@…) — real but
    not a person the owner would DM. Kept in the roster but demoted in name ranking."""
    e = email.lower()
    return any(h in e for h in (
        "read.ai", "otter.ai", "fireflies", "executiveassistant@",
        "@e.read.ai", "calendly", "@csaiautomations", "-noreply@", "via.",
    ))


# ─────────────────────── roster upsert ─────────────────────────

def _upsert(con: sqlite3.Connection, email: str, name: str, source: str,
            chat_user_id: str = "", dm_space: str = "") -> None:
    email = email.strip().lower()
    if not email or _is_noise_email(email):
        return
    name = _clean_name(name, email)
    row = con.execute(f"SELECT name, chat_user_id, dm_space, seen FROM {_TABLE} "
                      "WHERE email=?", (email,)).fetchone()
    now = time.time()
    if row is None:
        con.execute(
            f"INSERT INTO {_TABLE}(email,name,chat_user_id,dm_space,source,seen,updated)"
            " VALUES (?,?,?,?,?,?,?)",
            (email, name, chat_user_id, dm_space, source, 1, now))
    else:
        # keep the best name (a real display name beats a derived one), fill ids,
        # bump the interaction counter
        best_name = row["name"] or name
        if name and "@" not in name and (not row["name"] or _looks_derived(row["name"])):
            best_name = name
        con.execute(
            f"UPDATE {_TABLE} SET name=?, chat_user_id=?, dm_space=?, seen=seen+1, updated=? "
            "WHERE email=?",
            (best_name, chat_user_id or row["chat_user_id"],
             dm_space or row["dm_space"], now, email))


def _looks_derived(name: str) -> bool:
    """A derived name is Title-cased words; a real one may have other casing.
    Used only to prefer a real display name over a pattern guess."""
    return bool(name) and name == " ".join(w.capitalize() for w in name.split())


# ─────────────────────── roster building ───────────────────────

async def _harvest_gmail(con: sqlite3.Connection, max_msgs: int = 120) -> int:
    """Names from Gmail From: headers over recent mail (both directions)."""
    from tools import gog_workspace as gw
    n = 0
    for q in ("in:inbox newer_than:60d", "in:sent newer_than:60d"):
        rows = await gw.search_emails(q, max_msgs)
        for m in rows or []:
            frm = m.get("from", "") or ""
            mt = _FROM_RE.match(frm)
            if not mt:
                continue
            name, email = mt.group(1), mt.group(2)
            _upsert(con, email, name, "gmail")
            n += 1
    return n


async def _harvest_calendar(con: sqlite3.Connection, max_events: int = 40) -> int:
    """Emails (and any displayName) from calendar attendees + organizers."""
    from tools import gog_workspace as gw
    n = 0
    evs = await gw.upcoming_events(max_events)
    # also look back: past meetings are where most colleagues appear
    for e in evs or []:
        people = list(e.get("attendees") or [])
        org = e.get("organizer")
        if isinstance(org, dict):
            people.append(org)
        for a in people:
            if not isinstance(a, dict):
                continue
            email = a.get("email", "")
            if not email or a.get("self"):
                continue
            _upsert(con, email, a.get("displayName", ""), "calendar")
            n += 1
    return n


async def build_roster(force: bool = False) -> dict:
    """Refresh the people roster from Gmail + Calendar. Cheap, read-only, cached."""
    ensure_home()
    con = _db()
    _ensure(con)
    if not force:
        last = con.execute(f"SELECT MAX(updated) AS u FROM {_TABLE}").fetchone()
        if last and last["u"] and (time.time() - last["u"]) < _STALE_AFTER:
            cnt = con.execute(f"SELECT COUNT(*) AS c FROM {_TABLE}").fetchone()["c"]
            return {"cached": True, "people": cnt}
    g = c = 0
    try:
        g = await _harvest_gmail(con)
    except Exception as e:
        logger.debug("gmail harvest failed: %s", e)
    try:
        c = await _harvest_calendar(con)
    except Exception as e:
        logger.debug("calendar harvest failed: %s", e)
    con.commit()
    total = con.execute(f"SELECT COUNT(*) AS c FROM {_TABLE}").fetchone()["c"]
    logger.info("people roster: %d rows (gmail hits=%d, calendar hits=%d)", total, g, c)
    return {"cached": False, "people": total, "gmail": g, "calendar": c}


# ─────────────────────────── lookups ───────────────────────────

def _rows() -> list[sqlite3.Row]:
    con = _db()
    _ensure(con)
    return con.execute(
        f"SELECT email,name,chat_user_id,dm_space,seen FROM {_TABLE}").fetchall()


def resolve_name(query: str, limit: int = 3) -> list[dict]:
    """Fuzzy 'Jordan' / 'jordan park' / partial -> ranked people.

    Ranks: exact/substring name match first, then fuzzy ratio, tie-broken by
    interaction count (seen) so the person the owner actually talks to wins.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    scored: list[tuple[float, int, dict]] = []
    for r in _rows():
        name = (r["name"] or "").lower()
        email = (r["email"] or "").lower()
        if not name and not email:
            continue
        score = 0.0
        if q == name:
            score = 1.0
        elif q in name or (name and name.split()[0] == q):
            score = 0.9
        elif q in email.split("@")[0]:
            score = 0.8
        else:
            score = SequenceMatcher(None, q, name).ratio()
        # demote bot/relay addresses so a real human outranks an automated alias
        if _is_bot_email(email):
            score -= 0.25
        if score >= 0.6:
            scored.append((score, r["seen"] or 0, {
                "name": r["name"], "email": r["email"],
                "chat_user_id": r["chat_user_id"], "dm_space": r["dm_space"]}))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [d for _, _, d in scored[:limit]]


def name_for_email(email: str) -> str:
    """email -> display name (derived if unknown)."""
    email = (email or "").strip().lower()
    if not email:
        return ""
    con = _db(); _ensure(con)
    r = con.execute(f"SELECT name FROM {_TABLE} WHERE email=?", (email,)).fetchone()
    if r and r["name"]:
        return r["name"]
    return _name_from_email(email)


def name_for_chat_id(user_id: str) -> str:
    """Google Chat users/<id> -> display name, if we've correlated it."""
    uid = (user_id or "").strip()
    if not uid:
        return ""
    con = _db(); _ensure(con)
    r = con.execute(f"SELECT name FROM {_TABLE} WHERE chat_user_id=?", (uid,)).fetchone()
    return r["name"] if r and r["name"] else ""


def label_for_space(space_id: str, fallback: str = "") -> str:
    """spaces/<id> -> a human label if it's a known 1:1 DM, else the fallback."""
    sid = (space_id or "").strip()
    if not sid:
        return fallback or sid
    con = _db(); _ensure(con)
    r = con.execute(f"SELECT name FROM {_TABLE} WHERE dm_space=?", (sid,)).fetchone()
    if r and r["name"]:
        return r["name"]
    return fallback or sid


def link_chat_id(user_id: str, email: str = "", dm_space: str = "") -> None:
    """Record a Chat users/<id> ↔ email/space correlation once discovered."""
    con = _db(); _ensure(con)
    if email:
        _upsert(con, email, "", "manual", chat_user_id=user_id, dm_space=dm_space)
        con.commit()


async def dm_space_for_name(query: str) -> dict:
    """'Jordan' -> the DM space to send to (name -> email -> gog dm space).

    Returns {name, email, space} or {error}. Resolves the name, then asks gog
    for the DM space by email (creating the mapping if needed). This is what
    powers "reply to Jordan".
    """
    matches = resolve_name(query, limit=1)
    if not matches:
        # roster may be empty/stale — try one refresh, then re-resolve
        await build_roster()
        matches = resolve_name(query, limit=1)
    if not matches:
        return {"error": f"no one matching '{query}' in the roster"}
    person = matches[0]
    if person.get("dm_space"):
        return {"name": person["name"], "email": person["email"], "space": person["dm_space"]}
    email = person.get("email", "")
    if not email:
        return {"error": f"found {person.get('name')} but no email on file"}
    # ask gog for the DM space by email — the email is the sole positional
    # (find/setup are NOT passed; `gog chat dm space <email>` returns the space)
    from tools import gog_workspace as gw
    r = await gw._gog("chat", "dm", "space", email)
    space = ""
    if isinstance(r, dict):
        sp = r.get("space")
        if isinstance(sp, dict):
            space = sp.get("name") or sp.get("id") or ""
        elif isinstance(sp, str):
            space = sp
        space = space or r.get("name") or r.get("id") or ""
    if space:
        link_chat_id(person.get("chat_user_id", ""), email=email, dm_space=space)
        return {"name": person["name"], "email": email, "space": space}
    return {"name": person["name"], "email": email,
            "error": "couldn't resolve a DM space for that email"}
