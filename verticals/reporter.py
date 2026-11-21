"""Reporter — daily + 3-hourly digest of done / pending / failed / upcoming.

Reads state produced by other verticals (work log, action queue, learning
stats) rather than re-crawling sources, so it stays cheap. Rendered as Markdown
for Telegram; the router mirrors it to WhatsApp.
"""

from __future__ import annotations

from conductor.loop import recent_work
from approval import action_queue as aq


def _is_non_event(summary: str) -> bool:
    """A routine no-op scan ('no meetings', 'triaged 0') reported as Done ✅ is exactly
    the repetitive noise the owner rejects. Shared with the heartbeat's filter."""
    low = (summary or "").strip().lower()
    return any(p in low for p in ("no meeting", "no transcript", "no_actionable",
               "no actionable", "nothing to", "triaged 0", "0 items", "no new",
               "skipped", "up to date", "already", "meeting scan"))


def build_report(window_hours: int = 24, since_last: bool = True) -> str:
    from conductor import kv
    from datetime import datetime, timezone
    # gap-5 fix: the recurring 3h report used a flat 24h window with NO 'since last report'
    # anchor, so each Done item reappeared in up to 8 consecutive reports. Window from the
    # last report instead (min the requested window), so each item shows ~once.
    eff_hours = window_hours
    if since_last:
        last = (kv.get("reporter", {}) or {}).get("last_at")
        if last:
            try:
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                eff_hours = max(0.5, min(window_hours, age_h))
            except ValueError:
                pass
    work = recent_work(hours=eff_hours)
    # Only truly-completed work counts as done; drop non-events (gap-8).
    done = [w for w in work if w.get("status") in ("ok", "executed")
            and not _is_non_event(w.get("summary", w.get("task", "")))]
    awaiting = [w for w in work if w.get("status") in ("proposed_pr", "awaiting_approval")]
    failed = [w for w in work if w.get("status") == "error" or "fail" in str(w.get("status", ""))]
    pending = aq.get_pending_actions()
    stats = aq.get_action_stats()

    span = "24h" if window_hours >= 24 else f"{window_hours}h"
    lines = [f"*Sentinel report* — last {span}", ""]

    lines.append(f"✅ *Done* ({len(done)})")
    for w in done[:10]:
        lines.append(f"  • {w.get('kind','')}: {w.get('summary','')[:90]}")
    if not done:
        lines.append("  • (nothing completed)")

    lines.append("")
    lines.append(f"⏳ *Awaiting your approval* ({len(pending) + len(awaiting)})")
    for a in pending[:10]:
        crit = "🔴" if a.get("criticality") == "critical" else "🟡"
        lines.append(f"  {crit} `{a['id'].replace('action-','')}` {a.get('description','')[:80]}")
    for w in awaiting[:10]:
        lines.append(f"  ⏳ {w.get('kind','')}: {w.get('summary','')[:80]}")
    if not pending and not awaiting:
        lines.append("  • (none)")

    if failed:
        lines.append("")
        lines.append(f"⚠️ *Failed / needs attention* ({len(failed)})")
        for w in failed[:8]:
            lines.append(f"  • {w.get('kind','')}: {w.get('summary','')[:90]}")

    # chat actions Sentinel took for the owner (from the evidence-gated ledger) — real
    # deliveries carry evidence, so this is provable, not self-reported.
    try:
        from memory import ledger
        acts = ledger.recent(days=window_hours / 24, limit=8, status="done")
        if acts:
            lines.append("")
            lines.append(f"💬 *Handled in chat* ({len(acts)})")
            for a in acts[:8]:
                tgt = f" → {a['target']}" if a.get("target") else ""
                lines.append(f"  • {a['tool']}{tgt}")
    except Exception:
        pass

    # cost — how much Sentinel has spent (The owner asked for this in the 3h update)
    try:
        from verticals import cost
        lines.append("")
        lines.extend(cost.report_block())
    except Exception:
        pass

    lines.append("")
    lines.append(
        f"_approvals: {stats['approved']}✓ / {stats['rejected']}✗ / "
        f"{stats['pending']} pending · rate {stats['approval_rate']:.0%}_"
    )
    return "\n".join(lines)
