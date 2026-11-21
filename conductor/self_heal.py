"""Self-healing — the army detects its own failures and recovers, bounded.

Research doc 11 §c: recovery as a CONTROL problem owned by deterministic Python
(never LLM self-management — our load-bearing principle). Per task:

  detect -> classify -> bounded-recover (budget <=3) -> verify -> escalate|continue

Failure taxonomy + recovery recipe:
  transient-tool-error -> retry w/ backoff + jitter (+ GLOBAL circuit breaker)
  timeout              -> retry once, then escalate
  empty/low-signal     -> re-invoke once with an explicit "be concrete" nudge
  regression           -> Reflexion: capture failing output, write reflection,
                          re-invoke ONCE with it appended (bounded, not a loop)
  tool-broken          -> route around + propose a patch (gated); don't self-patch live
  refusal/irreversible -> STOP, escalate to human (Rule-of-Two)
  stuck-loop           -> break, replan or escalate

Doom-loop detection: fingerprint (tool + normalized args); trip at 3 identical.
Circuit breaker is GLOBAL across worktrees (per-execution backoff is the classic
bug that DDoSes your own downstream).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from conductor import kv

logger = logging.getLogger("self-heal")

DOOM_THRESHOLD = 3          # identical (tool,args) repeats before tripping
RECOVERY_BUDGET = 3         # max recovery actions per task
BREAKER_COOLDOWN_S = 300    # a tripped breaker stays open this long


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------- failure taxonomy

def classify(signal: dict) -> str:
    """signal: {exit_code, output, error, kind}. Returns a failure class."""
    err = (signal.get("error") or "").lower()
    out = (signal.get("output") or "")
    if signal.get("kind") == "refusal" or "refus" in err:
        return "refusal"
    if signal.get("kind") == "regression" or "regression" in err or "new test failure" in err:
        return "regression"
    if signal.get("kind") == "timeout" or "timed out" in err or "timeout" in err:
        return "timeout"
    if signal.get("kind") == "stuck" or "doom" in err or "loop" in err:
        return "stuck-loop"
    if re.search(r"(command not found|no such tool|not installed|broken pipe|importerror)", err):
        return "tool-broken"
    if not out.strip() and not err:
        return "empty"
    if signal.get("exit_code") not in (0, None) or err:
        return "transient-tool-error"
    return "ok"


RECOVERY = {
    "transient-tool-error": "retry",
    "timeout": "retry_once",
    "empty": "nudge",
    "regression": "reflexion",
    "tool-broken": "route_around",
    "refusal": "escalate",
    "stuck-loop": "escalate",
}


def recipe(failure_class: str) -> str:
    return RECOVERY.get(failure_class, "escalate")


# --------------------------------------------------------- doom-loop / breaker

def fingerprint(tool: str, args) -> str:
    norm = re.sub(r"\s+", " ", str(args)).strip().lower()[:200]
    return hashlib.sha1(f"{tool}|{norm}".encode()).hexdigest()[:12]


def observe_action(task_id: str, tool: str, args) -> bool:
    """Record a tool call; return True if this is a doom loop (trip the breaker)."""
    fp = fingerprint(tool, args)
    ring = kv.get("doom_rings", {}) or {}
    seq = ring.get(task_id, [])
    seq.append(fp)
    seq = seq[-6:]
    ring[task_id] = seq
    kv.put("doom_rings", ring)
    if seq[-DOOM_THRESHOLD:].count(fp) >= DOOM_THRESHOLD and len(seq) >= DOOM_THRESHOLD:
        trip_breaker(f"doom:{fp}")
        trip_breaker(f"task:{task_id}")   # also block this task's recovery budget
        return True
    return False


def trip_breaker(key: str) -> None:
    breakers = kv.get("breakers", {}) or {}
    breakers[key] = _now().isoformat()
    kv.put("breakers", dict(list(breakers.items())[-100:]))
    logger.warning("circuit breaker tripped: %s", key)


def breaker_open(key: str) -> bool:
    breakers = kv.get("breakers", {}) or {}
    ts = breakers.get(key)
    if not ts:
        return False
    try:
        return (_now() - datetime.fromisoformat(ts)).total_seconds() < BREAKER_COOLDOWN_S
    except ValueError:
        return False


def clear_task(task_id: str) -> None:
    ring = kv.get("doom_rings", {}) or {}
    ring.pop(task_id, None)
    kv.put("doom_rings", ring)


# --------------------------------------------------------------- recovery run

class RecoveryBudget:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.used = 0

    def can_recover(self) -> bool:
        return self.used < RECOVERY_BUDGET and not breaker_open(f"task:{self.task_id}")

    def spend(self) -> None:
        self.used += 1
        if self.used >= RECOVERY_BUDGET:
            trip_breaker(f"task:{self.task_id}")


def reflection_line(failing_output: str, objective: str) -> str:
    """The 1-sentence Reflexion note appended to the retry (research doc 11 §a#5)."""
    tail = (failing_output or "")[-500:]
    return (f"PRIOR ATTEMPT FAILED. Objective was: {objective[:150]}. "
            f"The failing signal was:\n{tail}\n"
            f"Reflect in one sentence on WHY it failed and what to change, then "
            f"produce a corrected attempt. Do not repeat the same approach.")


async def recover(task_id: str, objective: str, signal: dict, retry_fn,
                  budget: "RecoveryBudget | None" = None) -> dict:
    """Given a failure signal and an async retry_fn(extra_prompt)->result, apply
    the bounded recovery recipe. Returns {recovered, result, action, class}.

    retry_fn takes an optional extra prompt string (used by nudge/reflexion) and
    returns the same shape signal dict from a fresh attempt."""
    budget = budget or RecoveryBudget(task_id)
    cls = classify(signal)
    if cls == "ok":
        clear_task(task_id)
        return {"recovered": True, "result": signal, "action": "none", "class": cls}

    action = recipe(cls)
    logger.info("self-heal: task=%s class=%s action=%s (budget %d/%d)",
                task_id, cls, action, budget.used, RECOVERY_BUDGET)

    if action in ("escalate", "route_around") or not budget.can_recover():
        return {"recovered": False, "result": signal, "action": "escalate", "class": cls,
                "escalation": _escalation_text(objective, cls, signal)}

    budget.spend()
    extra = ""
    if action == "reflexion":
        extra = reflection_line(signal.get("output", "") + signal.get("error", ""), objective)
    elif action == "nudge":
        extra = "Your last output was empty or too vague. Be concrete and specific this time."
    # retry / retry_once / reflexion / nudge all re-invoke once with `extra`
    try:
        new = await retry_fn(extra)
    except Exception as e:
        return {"recovered": False, "result": {"error": str(e)}, "action": action,
                "class": cls, "escalation": _escalation_text(objective, cls, {"error": str(e)})}
    if classify(new) == "ok":
        clear_task(task_id)
        try:
            from memory import reasoning_bank
            reasoning_bank.record(objective, task_type="recovery", outcome="done",
                                  lesson=f"Recovered from {cls} via {action}.")
        except Exception:
            pass
        return {"recovered": True, "result": new, "action": action, "class": cls}
    # still failing -> record the failure as a guardrail and escalate
    try:
        from memory import reasoning_bank
        reasoning_bank.record_failure(objective, agent="self-heal",
                                      error=f"{cls}: {new.get('error', new.get('output',''))[:150]}",
                                      task_type="recovery")
    except Exception:
        pass
    return {"recovered": False, "result": new, "action": action, "class": cls,
            "escalation": _escalation_text(objective, cls, new)}


def _escalation_text(objective: str, cls: str, signal: dict) -> str:
    detail = (signal.get("error") or signal.get("output") or "")[:200]
    return (f"🛠️ I couldn't self-recover a task and stopped.\n"
            f"• Task: {objective[:100]}\n• Failure: {cls}\n• Detail: {detail}\n"
            f"Want me to try a different approach?")
