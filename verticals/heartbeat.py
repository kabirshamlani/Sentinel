"""3-hour heartbeat — Sentinel's periodic pulse to the owner.

Design rules (research doc 10 + openclaw): lead with Needs-you, then Done,
then Watching; hard 10-line cap; SKIP-IF-NOTHING is sacred — if there's
nothing needing him and nothing done, stay silent (one "alive" line max per
day). Quiet hours 23:30–07:30. Muted (/mute) skips too.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from conductor import kv

logger = logging.getLogger("heartbeat")

INTERVAL_HOURS = 3
QUIET_START, QUIET_END = 23.5, 7.5   # local time


def _local_hour() -> float:
    now = datetime.now()
    return now.hour + now.minute / 60


def in_quiet_hours() -> bool:
    h = _local_hour()
    return h >= QUIET_START or h < QUIET_END


def due() -> bool:
    if kv.muted() or in_quiet_hours():
        return False
    last = kv.get("heartbeat", {}).get("last_at")
    if not last:
        return True
    try:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        return elapsed >= INTERVAL_HOURS * 3600
    except ValueError:
        return True


async def build() -> str | None:
    """Compose the heartbeat, or None to skip (silence = health)."""
    from approval import action_queue as aq
    from conductor.loop import recent_work

    hb = kv.get("heartbeat", {}) or {}
    needs, done, watching = [], [], []

    # Needs you: pending approvals — but DON'T re-nag the same item every 3h (the owner's
    # complaint: "reporting the same thing again and again, I have to reject all").
    # Escalation ladder per action: surface it ONCE when new, then ONCE more at ~24h,
    # then go SILENT (it still lives in /tasks + the full report; the heartbeat stops
    # nagging). Tracked in hb['nagged'] = {action_id: highest_stage_shown}.
    nagged = hb.get("nagged", {}) or {}
    fresh_nag = dict(nagged)
    for a in aq.get_pending_actions():
        aid = a.get("id", "")
        age_h = _age_hours(a.get("proposed_at"))
        stage = 1 if age_h < 24 else (2 if age_h < 72 else 3)   # 3 = past-nagging
        shown = nagged.get(aid, 0)
        if stage >= 3 or stage <= shown:
            continue                      # already nagged at this stage (or older) — stay quiet
        fresh_nag[aid] = stage
        crit = "🔴" if a.get("criticality") == "critical" else "🟡"
        tag = "" if stage == 1 else " (still waiting — last nudge)"
        needs.append(f"{crit} {a.get('description','')[:70]} — {age_h:.0f}h{tag} "
                     f"(`approve {a['id'].replace('action-','')}`)")
        if len(needs) >= 3:
            break
    # prune nag memory to only still-pending ids so it doesn't grow unbounded
    pending_ids = {a.get("id") for a in aq.get_pending_actions()}
    fresh_nag = {k: v for k, v in fresh_nag.items() if k in pending_ids}
    hb["nagged"] = fresh_nag

    # Done since last heartbeat
    since_h = INTERVAL_HOURS
    if hb.get("last_at"):
        since_h = max(INTERVAL_HOURS, _age_hours(hb["last_at"]))
    for w in (recent_work(hours=since_h) or []):
        status = w.get("status", "")
        summary = (w.get("summary", w.get("task", "")) or "").strip()
        # skip NON-EVENTS — reporting "no meetings", "no actionable issue", "triaged 0"
        # as Done ✅ is exactly the repetitive noise the owner rejects. Only real outcomes.
        low = summary.lower()
        if any(p in low for p in ("no meeting", "no transcript", "no_actionable",
                                  "no actionable", "nothing to", "triaged 0", "0 items",
                                  "no new", "skipped", "up to date", "already")):
            continue
        icon = "✅" if status in ("done", "proposed_pr", "executed", "logged") else "▫️"
        label = (w.get("vertical") or w.get("kind") or "").strip()
        done.append(f"{icon} {label + ': ' if label else ''}{summary[:70]}")
        if len(done) >= 4:
            break

    # Digest queue (proactive items that scored DIGEST instead of interrupt)
    digest = kv.get("digest_queue", []) or []
    for d in digest[:3]:
        watching.append(f"👀 {d.get('text','')[:80]}")

    # Ambient: active work signals
    try:
        from tools import observer
        ctx = kv.get("work_context") or {}
        cur = (ctx.get("cursor") or [])
        if cur:
            watching.append(f"👀 Cursor active: {cur[0].get('name') or 'session'}")
    except Exception:
        pass

    if not needs and not done:
        # nothing worth saying — skip (one alive-note per day max). Persist the pruned
        # nag memory even on a skip so stages don't reset.
        kv.put("heartbeat", hb)
        today = datetime.now().date().isoformat()
        if hb.get("alive_note_date") != today and _local_hour() >= 20:
            hb["alive_note_date"] = today
            kv.put("heartbeat", hb)
            return "🫀 Alive — all quiet today, nothing needed you."
        return None

    lines = [f"🫀 **Sentinel** · last {since_h:.0f}h"]
    if needs:
        lines.append("⚠️ **Needs you:**")
        lines.extend(needs)
    if done:
        lines.append("✅ **Done:**")
        lines.extend(done)
    if watching:
        lines.extend(watching[:2])
    # mission backlog: asks I noticed but judged below the act-now threshold —
    # surface ONCE per item (drained here), so nothing dies silently in kv.
    try:
        backlog = kv.get("mission_backlog", []) or []
        if backlog:
            lines.append("🎯 **Noticed (not started):**")
            for b in backlog[-3:]:
                lines.append(f"• {b.get('sender','?')}: {b.get('task','')[:90]} "
                             f"(urgency {b.get('urgency','?')}/10 — say 'do it' to start)")
            kv.put("mission_backlog", [])
    except Exception:
        pass
    # cost line (the owner: cost in the 3h update)
    try:
        from verticals import cost
        cl = cost.line()
        if cl:
            lines.append(cl)
    except Exception:
        pass
    kv.put("digest_queue", [])   # flushed into this heartbeat
    body = "\n".join(lines[:12])

    # never send an IDENTICAL heartbeat twice in a row (belt-and-suspenders against
    # any residual repetition). If the meaningful content is unchanged, skip.
    import hashlib
    sig = hashlib.md5("\n".join(needs + done).encode()).hexdigest()
    if sig == hb.get("last_sig") and not done:
        kv.put("heartbeat", hb)
        return None
    hb["last_sig"] = sig
    kv.put("heartbeat", hb)
    return body


def mark_sent() -> None:
    hb = kv.get("heartbeat", {}) or {}
    hb["last_at"] = datetime.now(timezone.utc).isoformat()
    kv.put("heartbeat", hb)


def _age_hours(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return 0.0
