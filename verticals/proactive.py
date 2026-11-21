"""Proactive engine — Sentinel notices, decides, and acts WITHOUT being asked.

Architecture (research doc 10; arXiv 2605.30152): rules detect candidates
(Tier 0, no LLM) → deterministic 0–10 score → chief-of-staff LLM gate for
survivors only → INTERRUPT / DIGEST / DROP. Hard interruption budget 3/day,
per-category cooldowns, 48h dedupe, auto-tuning from the owner's reactions
("less of this" raises that category's bar). The goal is the LOWEST possible
false-positive rate: silence is always acceptable, spam never.

What it can decide to do:
  * meeting_prep  — meeting in 45–90 min → prep notes pushed to Telegram
  * urgent_email  — unread mail that smells urgent → flag with context
  * stale_approval— critical approval waiting >12h → gentle reminder
  * idle_work     — nothing pending + open issues on watched repos → start one
                    (result arrives as a normal PR approval, not a ping)
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from conductor import kv

logger = logging.getLogger("proactive")

DAILY_INTERRUPT_BUDGET = 3
CATEGORY_COOLDOWN_H = 4
DEDUPE_WINDOW_H = 48
INTERRUPT_THRESHOLD = 6

_URGENT_RE = re.compile(r"\b(urgent|asap|action required|deadline|today|eod|blocker|"
                        r"failing|down|broken|p0|p1)\b", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------- Tier 0: rules

async def gather_candidates() -> list[dict]:
    cands: list[dict] = []
    cands += await _meeting_candidates()
    cands += await _email_candidates()
    cands += _approval_candidates()
    return cands


async def _meeting_candidates() -> list[dict]:
    try:
        from tools import gog_workspace as gw
        if not gw.available():
            return []
        events = await gw.upcoming_events(8)
    except Exception:
        return []
    out = []
    for e in events:
        e = gw._norm_event(e)
        start_raw = str(e.get("start", e.get("startTime", "")))[:19]
        try:
            start = datetime.fromisoformat(start_raw)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)  # best effort
        except ValueError:
            continue
        mins = (start - _now()).total_seconds() / 60
        if 40 <= mins <= 95:
            out.append({
                "category": "meeting_prep",
                "key": f"prep:{e.get('id', e.get('summary',''))}:{start_raw[:10]}",
                "text": f"{e.get('summary','meeting')} at {start_raw[11:16]}",
                "event": e,
                "urgency": 2, "consequence": 2, "confidence": 2,
                "actionability": 2, "novelty": 1, "timing": 1,
            })
    return out


async def _email_candidates() -> list[dict]:
    try:
        from tools import gog_workspace as gw
        if not gw.available():
            return []
        emails = await gw.search_emails("is:unread newer_than:1d", 10)
    except Exception:
        return []
    out = []
    for e in emails:
        subj = e.get("subject", "") or ""
        frm = e.get("from", "") or ""
        if _URGENT_RE.search(subj):
            out.append({
                "category": "urgent_email",
                "key": f"mail:{e.get('id', subj)}",
                "text": f"Unread & urgent-looking: {frm[:35]} — {subj[:60]}",
                "urgency": 2, "consequence": 1, "confidence": 1,
                "actionability": 2, "novelty": 1, "timing": 1,
            })
    return out[:3]


def _approval_candidates() -> list[dict]:
    from approval import action_queue as aq
    out = []
    for a in aq.get_pending_actions():
        if a.get("criticality") != "critical":
            continue
        try:
            age_h = (_now() - datetime.fromisoformat(a["proposed_at"])).total_seconds() / 3600
        except (KeyError, ValueError):
            continue
        if age_h >= 12:
            out.append({
                "category": "stale_approval",
                "key": f"stale:{a['id']}:{int(age_h // 12)}",
                "text": f"Still waiting on you ({age_h:.0f}h): {a.get('description','')[:70]} "
                        f"— `approve {a['id'].replace('action-','')}`",
                "urgency": 1, "consequence": 2, "confidence": 2,
                "actionability": 2, "novelty": 0, "timing": 1,
            })
    return out


# ---------------------------------------------------------- scoring + gates

def score(c: dict) -> int:
    s = (2 * c.get("urgency", 0) + 2 * c.get("consequence", 0) +
         2 * c.get("confidence", 0) + 2 * c.get("actionability", 0)) / 2
    s += c.get("novelty", 0) + c.get("timing", 0)
    stats = kv.get("proactive_stats", {}) or {}
    s -= stats.get(c["category"], {}).get("threshold_adj", 0)
    return int(s)


def _budget_left() -> int:
    today = datetime.now().date().isoformat()
    b = kv.get("interrupt_budget", {}) or {}
    if b.get("date") != today:
        b = {"date": today, "used": 0}
        kv.put("interrupt_budget", b)
    return DAILY_INTERRUPT_BUDGET - b.get("used", 0)


def _spend_budget() -> None:
    today = datetime.now().date().isoformat()
    b = kv.get("interrupt_budget", {}) or {}
    if b.get("date") != today:
        b = {"date": today, "used": 0}
    b["used"] = b.get("used", 0) + 1
    kv.put("interrupt_budget", b)


def _deduped(c: dict) -> bool:
    seen = kv.get("proactive_seen", {}) or {}
    h = hashlib.sha1(c["key"].encode()).hexdigest()[:12]
    last = seen.get(h)
    if last:
        try:
            if (_now() - datetime.fromisoformat(last)).total_seconds() < DEDUPE_WINDOW_H * 3600:
                return True
        except ValueError:
            pass
    seen[h] = _now().isoformat()
    kv.put("proactive_seen", dict(list(seen.items())[-200:]))
    return False


def _cooled_down(category: str) -> bool:
    stats = kv.get("proactive_stats", {}) or {}
    last = stats.get(category, {}).get("last_sent")
    if not last:
        return True
    try:
        return (_now() - datetime.fromisoformat(last)).total_seconds() >= CATEGORY_COOLDOWN_H * 3600
    except ValueError:
        return True


def _mark_sent(category: str) -> None:
    stats = kv.get("proactive_stats", {}) or {}
    cat = stats.setdefault(category, {})
    cat["last_sent"] = _now().isoformat()
    cat["sent"] = cat.get("sent", 0) + 1
    kv.put("proactive_stats", stats)


def tune_down(category: str | None = None) -> str:
    """The owner said 'less of this' — raise that category's bar by 2 (fewer pings)."""
    stats = kv.get("proactive_stats", {}) or {}
    category = category or stats.get("_last_category", "")
    if not category:
        return "Nothing recent to tune."
    cat = stats.setdefault(category, {})
    cat["threshold_adj"] = min(6, cat.get("threshold_adj", 0) + 2)
    cat["dismissed"] = cat.get("dismissed", 0) + 1
    kv.put("proactive_stats", stats)
    return f"Got it — raising the bar for {category} pings."


def tune_up(category: str | None = None) -> None:
    """POSITIVE signal — the owner engaged with a ping (approved/acted on it), so this
    category is welcome: lower its bar toward neutral (B5, the missing half). This
    is the learning-from-reactions the docstring always promised."""
    stats = kv.get("proactive_stats", {}) or {}
    category = category or stats.get("_last_category", "")
    if not category:
        return
    cat = stats.setdefault(category, {})
    cat["threshold_adj"] = max(-2, cat.get("threshold_adj", 0) - 1)   # can go slightly below neutral
    cat["accepted"] = cat.get("accepted", 0) + 1
    kv.put("proactive_stats", stats)


def note_engagement(strong: bool = False, within_minutes: int = 15) -> None:
    """POSITIVE signal — the owner engaged with a recent ping. To avoid gaming (any
    unrelated reply inflating a spammy category), we require a STRONG signal:
    `strong=True` is passed only when he APPROVES/acts on the pinged item (not
    merely sends some message). A weak/neutral inbound does NOT up-tune. Credits
    the most-recent pinged category if its ping was within the window."""
    if not strong:
        return
    stats = kv.get("proactive_stats", {}) or {}
    last_cat = stats.get("_last_category")
    last_sent = stats.get(last_cat, {}).get("last_sent") if last_cat else None
    if not last_cat or not last_sent:
        return
    try:
        from datetime import datetime
        age = (_now() - datetime.fromisoformat(last_sent)).total_seconds() / 60
        if age <= within_minutes:
            tune_up(last_cat)
    except (ValueError, TypeError):
        pass


def decay_tuning() -> None:
    """Nightly: relax every category's threshold_adj toward 0 by 1 step, so a
    once-muted category isn't penalized forever (B5 — the missing decay). Slow
    enough that repeated dismissals still dominate."""
    stats = kv.get("proactive_stats", {}) or {}
    changed = False
    for cat, d in stats.items():
        if isinstance(d, dict) and d.get("threshold_adj"):
            adj = d["threshold_adj"]
            d["threshold_adj"] = adj - 1 if adj > 0 else (adj + 1 if adj < 0 else 0)
            changed = True
    if changed:
        kv.put("proactive_stats", stats)


async def _chief_of_staff_gate(cands: list[dict]) -> dict[str, str]:
    """One cheap LLM call: INTERRUPT / DIGEST / DROP for each survivor."""
    if not cands:
        return {}
    from brain import claude_runner
    listing = "\n".join(f"{i+1}. [{c['category']}] {c['text']}" for i, c in enumerate(cands))
    prompt = (
        "You are a ruthless chief of staff guarding an executive's attention. For "
        "each item decide: INTERRUPT (walk into his office now), DIGEST (mention in "
        "the next periodic summary), or DROP (handle silently / not worth it). He "
        "hates noise; when in doubt, DIGEST.\n\n"
        f"{listing}\n\n"
        "Reply with one line per item: '<number>: INTERRUPT|DIGEST|DROP'"
    )
    out = ""
    try:
        async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                                 allowed_tools=[], model=None, max_turns=0):
            if ev.type == "result":
                out = ev.content or ""
    except Exception as e:
        logger.warning("gate failed (defaulting to DIGEST): %s", e)
    verdicts: dict[str, str] = {}
    for i, c in enumerate(cands):
        m = re.search(rf"^\s*{i+1}\s*[:.\-]\s*(INTERRUPT|DIGEST|DROP)", out, re.M | re.I)
        verdicts[c["key"]] = m.group(1).upper() if m else "DIGEST"
    return verdicts


# ------------------------------------------------------------------ actions

async def _do_meeting_prep(c: dict) -> str:
    """Generate handy notes for the upcoming meeting."""
    from brain import claude_runner
    from tools import observer
    event = c.get("event", {})
    ctx = observer.summary_text()
    prompt = (
        "Prepare crisp pre-meeting notes for the owner (he'll read them on his phone in "
        "2 minutes). Meeting: " + str({k: str(v)[:120] for k, v in event.items()})[:600] +
        f"\n\nHis current work context:\n{ctx}\n\n"
        "You may run read-only commands / read files if the meeting relates to a "
        "local project. Produce: 1) what this meeting is about, 2) 3 things worth "
        "saying/asking, 3) any open items he owes them. Max 10 lines, no fluff."
    )
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             allowed_tools=["Read", "Grep", "Glob", "Bash"],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    return f"📅 **{c['text']}** — your prep:\n{out}" if out else ""


# --------------------------------------------------------------- main cycle

async def run_cycle(router) -> dict:
    """Called by the daemon every ~30 min. Cheap unless something's up."""
    if kv.muted():
        return {"status": "muted"}
    from verticals.heartbeat import in_quiet_hours
    if in_quiet_hours():
        return {"status": "quiet_hours"}

    cands = [c for c in await gather_candidates() if not _deduped(c)]
    survivors = [c for c in cands if score(c) >= INTERRUPT_THRESHOLD and _cooled_down(c["category"])]
    dropped_low = [c for c in cands if c not in survivors]

    # low scorers with some value → digest queue
    dq = kv.get("digest_queue", []) or []
    for c in dropped_low:
        if score(c) >= 3:
            dq.append({"text": c["text"], "at": _now().isoformat()})
    kv.put("digest_queue", dq[-10:])

    if not survivors:
        return {"status": "quiet", "candidates": len(cands)}

    verdicts = await _chief_of_staff_gate(survivors)
    sent = 0
    for c in survivors:
        v = verdicts.get(c["key"], "DIGEST")
        if v == "INTERRUPT" and _budget_left() > 0:
            msg = c["text"]
            if c["category"] == "meeting_prep":
                msg = await _do_meeting_prep(c) or msg
            await router.broadcast(msg + "\n\n_(say 'less of this' to tune these down)_")
            _spend_budget()
            _mark_sent(c["category"])
            stats = kv.get("proactive_stats", {}) or {}
            stats["_last_category"] = c["category"]
            kv.put("proactive_stats", stats)
            sent += 1
        elif v in ("DIGEST", "INTERRUPT"):   # INTERRUPT over budget → digest
            dq = kv.get("digest_queue", []) or []
            dq.append({"text": c["text"], "at": _now().isoformat()})
            kv.put("digest_queue", dq[-10:])
    logger.info("proactive: %d candidates, %d interrupts", len(cands), sent)
    return {"status": "ok", "candidates": len(cands), "interrupted": sent}
