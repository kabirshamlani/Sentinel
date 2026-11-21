"""Idle experiments — Sentinel never sits idle; it runs safe experiments.

"agents should not sit ideal, it should do some or the other things, do some
experiments" — when the machine is free and nothing is pending, run ONE
experiment from the queue (cap 2/day). Experiments are Tier-R by construction:
model races on fixture tasks, self-benchmarks, research probes. Results feed
the model ledger + insights notebook — this is how the system improves itself
with evidence instead of vibes.
"""

from __future__ import annotations

import logging
from datetime import datetime

from conductor import kv

logger = logging.getLogger("experiments")

DAILY_CAP = 2

_SEED_QUEUE = [
    {"kind": "race", "task_type": "coding",
     "prompt": "Write a Python function `dedupe_preserving_order(items)` that removes "
               "duplicates from a list while preserving first-seen order, with type hints, "
               "docstring, and 3 doctest examples. Then explain its complexity in one line."},
    {"kind": "race", "task_type": "summarize",
     "prompt": "Summarize for a busy engineer in exactly 5 bullets: the tradeoffs between "
               "polling, webhooks, and websockets for receiving third-party events, "
               "including failure modes and when each wins."},
    {"kind": "race", "task_type": "classify",
     "prompt": "Classify each message as WORK or PERSONAL, one per line:\n"
               "1. 'deploy failed on prod, rolling back'\n2. 'bro did you see the match'\n"
               "3. 'sprint review moved to 3pm'\n4. 'dinner saturday?'\n"
               "5. 'PR #42 needs your review'"},
    {"kind": "race", "task_type": "reasoning",
     "prompt": "A cron job ran at 02:00 daily and silently stopped after a DST change on a "
               "server set to Asia/Kolkata (no DST). List the 3 most likely root causes in "
               "order of probability and the single fastest check for each."},
]


def _today() -> str:
    return datetime.now().date().isoformat()


def _runs_today() -> int:
    st = kv.get("experiments", {}) or {}
    return st.get("count", 0) if st.get("date") == _today() else 0


def _mark_run() -> None:
    st = kv.get("experiments", {}) or {}
    if st.get("date") != _today():
        st = {"date": _today(), "count": 0, "cursor": st.get("cursor", 0)}
    st["count"] = st.get("count", 0) + 1
    kv.put("experiments", st)


def can_run() -> bool:
    return _runs_today() < DAILY_CAP


async def run_next(router=None) -> dict | None:
    """Pop the next experiment and run it. Called by the daemon when idle."""
    if not can_run():
        return None
    st = kv.get("experiments", {}) or {}
    cursor = st.get("cursor", 0)
    exp = _SEED_QUEUE[cursor % len(_SEED_QUEUE)]
    st["cursor"] = cursor + 1
    kv.put("experiments", st)
    _mark_run()

    if exp["kind"] == "race":
        from brain import arena
        res = await arena.race(exp["prompt"], task_type=exp["task_type"])
        summary = (f"experiment: raced models on {exp['task_type']} — winner "
                   f"{res.get('winner', '?')}" if not res.get("error")
                   else f"experiment failed: {res['error'][:100]}")
        try:
            from learning import insights
            if res.get("winner"):
                detail = res.get("verdict_detail") or {}
                insights.append_insight(
                    f"Model race ({exp['task_type']}): {res['winner']} won "
                    f"({detail.get('reason', 'n/a')})",
                    category="models", evidence=f"race_id={res.get('race_id')}",
                    source="experiments")
        except Exception:
            pass
        from conductor.loop import log_work
        log_work("experiments", summary, "done", {"race_id": res.get("race_id")})
        logger.info(summary)
        return res
    return None
