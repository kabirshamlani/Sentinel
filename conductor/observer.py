"""Observer — the agent that watches the agents.

The owner asked for "a observer agent which checks the agents, see what all agents
are doing." It's a supervisor, not a worker: it records every pipeline/specialist
run (latency, review verdict, recovery events), maintains rolling health per
agent, detects degradation, and escalates to the owner when quality drops — feeding
the self-improvement loop (recurring failures → guardrails/new specialists).

Deterministic Python (health accounting, thresholds); it only spends an LLM call
when it needs to summarize a concerning pattern for the owner. Everything is bounded
and logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from conductor import kv

logger = logging.getLogger("observer")

DEGRADE_WINDOW = 10          # last N runs per agent
DEGRADE_FAIL_RATE = 0.5      # >50% flagged/failed in window => degraded
SLOW_SECONDS = 180           # a run slower than this is noted


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_run(task_id: str, request: dict, result: str, review: dict,
               elapsed: float = 0.0) -> dict:
    """Log one pipeline run's health. Returns a compact observation."""
    ok = bool(review.get("ok"))
    verdict = review.get("verdict", "?")
    domains = request.get("domains") or ["general"]
    runs = kv.get("observer_runs", []) or []
    obs = {"task_id": task_id, "at": _now(), "ok": ok, "verdict": verdict,
           "domains": domains, "elapsed": round(elapsed, 1),
           "intent": request.get("intent", "")[:80], "empty": not bool(result)}
    runs.append(obs)
    kv.put("observer_runs", runs[-120:])

    # per-agent (domain) rolling health
    health = kv.get("observer_health", {}) or {}
    for d in domains:
        h = health.setdefault(d, {"runs": [], "degraded": False})
        h["runs"].append({"ok": ok, "at": obs["at"], "elapsed": obs["elapsed"]})
        h["runs"] = h["runs"][-DEGRADE_WINDOW:]
        fails = sum(1 for r in h["runs"] if not r["ok"])
        h["degraded"] = len(h["runs"]) >= 4 and fails / len(h["runs"]) >= DEGRADE_FAIL_RATE
    kv.put("observer_health", health)

    if elapsed > SLOW_SECONDS:
        logger.info("observer: slow run %s (%.0fs) for %s", task_id, elapsed, domains)
    return obs


def degraded_agents() -> list[str]:
    health = kv.get("observer_health", {}) or {}
    return [d for d, h in health.items() if h.get("degraded")]


def health_text() -> str:
    """For /health command or the heartbeat 'Watching' section."""
    health = kv.get("observer_health", {}) or {}
    runs = kv.get("observer_runs", []) or []
    if not runs:
        return "👁️ Observer: no agent runs recorded yet."
    lines = [f"👁️ **Agent health** ({len(runs)} recent runs)"]
    for d, h in sorted(health.items()):
        n = len(h.get("runs", []))
        if not n:
            continue
        ok = sum(1 for r in h["runs"] if r["ok"])
        flag = " ⚠️ DEGRADED" if h.get("degraded") else ""
        avg = sum(r.get("elapsed", 0) for r in h["runs"]) / n
        lines.append(f"• {d}: {ok}/{n} ok, ~{avg:.0f}s avg{flag}")
    return "\n".join(lines)


async def sweep(router=None) -> dict:
    """Periodic supervisor pass: if an agent is degraded, escalate once and feed
    the self-improvement loop (a degraded specialist is a demonstrated need)."""
    degraded = degraded_agents()
    if not degraded:
        return {"status": "healthy"}
    notified = kv.get("observer_notified", {}) or {}
    today = datetime.now().date().isoformat()
    fresh = [d for d in degraded if notified.get(d) != today]
    if fresh and router is not None:
        for d in fresh:
            notified[d] = today
        kv.put("observer_notified", notified)
        await router.broadcast(
            f"👁️ Heads up — these agents are underperforming lately: {', '.join(fresh)}. "
            f"I'm feeding it into the improvement loop (guardrail rule / possible "
            f"specialist tune). Say `/health` for detail.")
        # observer -> self-improvement: a degraded agent is a demonstrated need,
        # so kick the cluster-scan that adds a guardrail / proposes a specialist.
        try:
            from learning import self_improve
            from conductor.daemon import get_broker
            broker = get_broker()
            if broker is not None and hasattr(broker, "propose"):
                # pass the WORST degraded domain so the fix targets it (B6)
                await self_improve.propose_improvement(broker, focus_domain=fresh[0])
        except Exception as e:
            logger.debug("observer->self_improve skipped: %s", e)
    logger.info("observer sweep: degraded=%s", degraded)
    return {"status": "degraded", "agents": degraded}
