"""Cost tracker — how much has Sentinel spent, surfaced in chat + the 3h report.

The per-call cost telemetry already lands in kv['cost_ledger'] (today only, reset each
day by claude_runner._record_cost). This adds:
  - a PERSISTENT multi-day history (kv['cost_history']) so spend isn't lost at midnight,
  - one-line + detailed summaries the heartbeat and reporter render,
  - a 7-day / all-time rollup so the owner sees cumulative burn, not just today.

The owner: "add a cost tracker that tells me how much cost has been consumed, along with the
daily updates in 3 hrs." Cost is informational here — the daily BUDGET gate lives in the
daemon (_budget_ok); this module only reports.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from conductor import kv


def _roll_history() -> dict:
    """Fold the current daily ledger into the persistent history keyed by date.
    Idempotent: overwrites today's row with the latest running total."""
    led = kv.get("cost_ledger", {}) or {}
    hist = kv.get("cost_history", {}) or {}
    d = led.get("date")
    if d and led.get("runs"):
        hist[d] = {"cost_usd": led.get("cost_usd", 0.0), "runs": led.get("runs", 0),
                   "in": led.get("in", 0), "out": led.get("out", 0)}
        # keep 60 days max
        for old in sorted(hist)[:-60]:
            hist.pop(old, None)
        kv.put("cost_history", hist)
    return hist


def today() -> dict:
    return kv.get("cost_ledger", {}) or {}


def window_total(days: int = 7) -> dict:
    """Sum spend over the last N days (from history + today's live ledger)."""
    hist = _roll_history()
    cutoff = (datetime.now().date() - timedelta(days=days - 1)).isoformat()
    total = {"cost_usd": 0.0, "runs": 0, "in": 0, "out": 0, "days": 0}
    for d, row in hist.items():
        if d >= cutoff:
            total["cost_usd"] += row.get("cost_usd", 0.0)
            total["runs"] += row.get("runs", 0)
            total["in"] += row.get("in", 0)
            total["out"] += row.get("out", 0)
            total["days"] += 1
    total["cost_usd"] = round(total["cost_usd"], 4)
    return total


def all_time() -> dict:
    hist = _roll_history()
    return {"cost_usd": round(sum(r.get("cost_usd", 0.0) for r in hist.values()), 4),
            "runs": sum(r.get("runs", 0) for r in hist.values()),
            "days": len(hist)}


def line() -> str:
    """One-liner for the heartbeat: today + 7-day, with the daily budget headroom."""
    t = today()
    if not t.get("runs"):
        return ""
    wk = window_total(7)
    from conductor.daemon import SentinelDaemon
    budget = getattr(SentinelDaemon, "DAILY_BUDGET_USD", 15.0)
    return (f"💸 ${t.get('cost_usd',0):.2f} today ({t.get('runs',0)} runs) · "
            f"${wk['cost_usd']:.2f}/7d · budget ${budget:.0f}/day")


def report_block() -> list[str]:
    """Multi-line block for the 3h report: today by-agent + 7d + all-time."""
    t = today()
    if not t.get("runs"):
        return ["💸 *Cost*: no model spend today"]
    wk = window_total(7)
    at = all_time()
    lines = [f"💸 *Cost* — today ${t.get('cost_usd',0):.3f} over {t.get('runs',0)} runs "
             f"({t.get('in',0)//1000}k in / {t.get('out',0)//1000}k out)"]
    top = sorted(t.get("by_agent", {}).items(), key=lambda x: -x[1])[:4]
    if top:
        lines.append("  " + " · ".join(f"{a} ${c:.2f}" for a, c in top))
    lines.append(f"  7-day ${wk['cost_usd']:.2f} · all-time ${at['cost_usd']:.2f} "
                 f"over {at['days']}d")
    return lines
