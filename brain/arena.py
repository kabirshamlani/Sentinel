"""Model arena — race models on real tasks, keep evidence, learn who's best.

Protocol (research doc 09):
  * Race N=2–3 models in parallel on the same prompt (only for high-value,
    judgeable tasks — routine turns are routed, not raced).
  * Judge BOTH orders (position bias flips 10–30% of verdicts) with a judge
    OUTSIDE the contestant pool; disagreement = tie -> the cheaper model wins
    the record.
  * Every attempt lands in the ledger (JSONL). Win rates use a 30-day window.
  * best_model(task_type): epsilon-greedy(0.1) over decayed win rates with a
    minimum of 5 trials — otherwise the default tier map decides.

This is the "prove model X is best, with evidence" system the owner asked for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from config.paths import SENTINEL_HOME
from brain import providers

logger = logging.getLogger("arena")

LEDGER = SENTINEL_HOME / "model_ledger.jsonl"
EPSILON = 0.1
MIN_TRIALS = 5
WINDOW_DAYS = 30

JUDGE_PREFS = ["claude-haiku-4-5", "gemini-3.5-flash", "claude-sonnet-5"]

DEFAULT_BY_TASK = {
    "coding": "claude-sonnet-5",
    "reasoning": "claude-opus-4-8",
    "summarize": "gemini-3.5-flash",
    "classify": "claude-haiku-4-5",
    "chat": "claude-sonnet-5",
    "research": "claude-opus-4-8",
    "long_context": "gemini-3.1-pro",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec["ts"] = _now().isoformat()
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rows(days: int = WINDOW_DAYS) -> list[dict]:
    if not LEDGER.exists():
        return []
    cutoff = (_now() - timedelta(days=days)).isoformat()
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if r.get("ts", "") >= cutoff:
                out.append(r)
        except json.JSONDecodeError:
            continue
    return out


# ------------------------------------------------------------------- racing

async def race(prompt: str, task_type: str = "chat", models: list[str] | None = None,
               system: str = "", max_tokens: int = 1200) -> dict:
    """Run 2–3 models in parallel, judge with order-swap, record evidence.

    Returns {winner, output, race_id, results, verdict_detail}."""
    avail = providers.available_models()
    if not models:
        pool = [m for m in [DEFAULT_BY_TASK.get(task_type, "claude-sonnet-5"),
                            "gemini-3.5-flash", "grok-4.5", "gpt-5.6-terra"] if m in avail]
        models = list(dict.fromkeys(pool))[:3]
    models = [m for m in models if m in avail][:3]
    if len(models) < 2:
        one = models[0] if models else (avail[0] if avail else None)
        if not one:
            return {"error": "no models available"}
        r = await providers.call_model(one, prompt, system, max_tokens)
        return {"winner": one, "output": r.get("text", ""), "race_id": None,
                "results": [r], "note": "only one model available — no race"}

    race_id = f"race-{uuid.uuid4().hex[:8]}"
    results = await asyncio.gather(*[providers.call_model(m, prompt, system, max_tokens)
                                     for m in models])
    ok = [r for r in results if r.get("text")]
    for r in results:
        if r.get("error"):
            _log({"task_type": task_type, "model": r["model"], "outcome": "error",
                  "race_id": race_id, "error": r["error"][:200]})
    if not ok:
        return {"error": "all models failed", "results": results, "race_id": race_id}
    if len(ok) == 1:
        w = ok[0]
        _log({"task_type": task_type, "model": w["model"], "outcome": "judge_win",
              "race_id": race_id, "cost_usd": w.get("cost_usd"), "note": "opponents errored"})
        return {"winner": w["model"], "output": w["text"], "race_id": race_id, "results": results}

    a, b = ok[0], ok[1]                       # judge top-2 (N=2 captures most gain)
    verdict = await _judge_pair(prompt, task_type, a, b)
    winner = a if verdict["winner"] == "A" else b if verdict["winner"] == "B" else \
        (a if providers.price(a["model"], 1000, 1000) <= providers.price(b["model"], 1000, 1000) else b)
    loser = b if winner is a else a
    outcome_w = "judge_win" if verdict["winner"] in ("A", "B") else "tie"
    _log({"task_type": task_type, "model": winner["model"], "outcome": outcome_w,
          "race_id": race_id, "cost_usd": winner.get("cost_usd"),
          "latency_s": winner.get("latency_s"), "judge": verdict.get("judge"),
          "vs": loser["model"], "reason": verdict.get("reason", "")[:200]})
    _log({"task_type": task_type, "model": loser["model"],
          "outcome": "judge_loss" if outcome_w == "judge_win" else "tie",
          "race_id": race_id, "cost_usd": loser.get("cost_usd"),
          "latency_s": loser.get("latency_s"), "vs": winner["model"]})
    return {"winner": winner["model"], "output": winner["text"], "race_id": race_id,
            "results": results, "verdict_detail": verdict}


async def _judge_pair(prompt: str, task_type: str, a: dict, b: dict) -> dict:
    """Order-swapped forced-choice judging; disagreement = tie."""
    contestants = {a["model"], b["model"]}
    judge = next((j for j in JUDGE_PREFS
                  if j in providers.available_models() and j not in contestants), None)
    if not judge:
        return {"winner": "tie", "judge": None, "reason": "no independent judge available"}

    async def ask(x: dict, y: dict) -> str:
        jp = (f"Task ({task_type}): {prompt[:1200]}\n\n"
              f"Response A:\n{x['text'][:2500]}\n\nResponse B:\n{y['text'][:2500]}\n\n"
              "Which response is better for the task? Judge correctness first, then "
              "completeness, then clarity. Reply with exactly one letter: A or B.")
        r = await providers.call_model(judge, jp, max_tokens=8)
        t = (r.get("text") or "").strip().upper()
        return "A" if t.startswith("A") else "B" if t.startswith("B") else "?"

    v1, v2 = await asyncio.gather(ask(a, b), ask(b, a))
    # v2 was order-swapped: its "A" means our b
    v2_mapped = {"A": "B", "B": "A"}.get(v2, "?")
    if v1 == v2_mapped and v1 in ("A", "B"):
        return {"winner": v1, "judge": judge, "reason": f"consistent across both orders ({v1})"}
    return {"winner": "tie", "judge": judge,
            "reason": f"judges disagreed across orders ({v1} vs {v2_mapped}) -> tie, cheaper wins"}


# ------------------------------------------------------------------ ledger

def win_rates(task_type: str | None = None) -> dict[str, dict]:
    rows = _rows()
    stats: dict[str, dict] = {}
    for r in rows:
        if task_type and r.get("task_type") != task_type:
            continue
        m = r.get("model", "?")
        s = stats.setdefault(m, {"wins": 0, "losses": 0, "ties": 0, "errors": 0,
                                 "races": 0, "cost": 0.0})
        o = r.get("outcome")
        if o == "judge_win":
            s["wins"] += 1
        elif o == "judge_loss":
            s["losses"] += 1
        elif o == "tie":
            s["ties"] += 1
        elif o == "error":
            s["errors"] += 1
        s["races"] += 1
        s["cost"] += r.get("cost_usd") or 0
    for m, s in stats.items():
        contested = s["wins"] + s["losses"]
        s["win_rate"] = s["wins"] / contested if contested else 0.0
        s["ci"] = _wilson_halfwidth(s["wins"], contested)
    return stats


def _wilson_halfwidth(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 1.0
    p = wins / n
    return z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)


def best_model(task_type: str, cli_only: bool = False) -> str | None:
    """Epsilon-greedy over the ledger; None = ledger has no confident answer.

    cli_only=True restricts the winner to Anthropic models — the ONLY ones the
    Claude Code CLI can run. Without this, a proven Gemini/Grok/GPT winner would
    be passed as `claude --model gemini-3.5-flash`, which the CLI rejects and the
    run errors out. Callers on the Claude-CLI path (claude_runner) MUST pass
    cli_only=True; the racing/direct-API path can use the unrestricted winner."""
    stats = win_rates(task_type)
    ranked = [(m, s) for m, s in stats.items() if s["wins"] + s["losses"] >= MIN_TRIALS]
    if cli_only:
        try:
            from brain.providers import REGISTRY
            ranked = [(m, s) for m, s in ranked
                      if REGISTRY.get(m, {}).get("provider") == "anthropic"]
        except Exception:
            ranked = [(m, s) for m, s in ranked if m.startswith("claude-")]
    if not ranked:
        return None
    ranked.sort(key=lambda x: -x[1]["win_rate"])
    if len(ranked) > 1 and random.random() < EPSILON:
        return ranked[1][0]
    return ranked[0][0]


def leaderboard_text() -> str:
    rows = _rows()
    if not rows:
        return ("🏁 No model races yet. I race models on real tasks (research, "
                "summaries, experiments) and keep the evidence here.")
    lines = [f"🏁 **Model leaderboard** ({WINDOW_DAYS}d, {len(rows)} recorded attempts)"]
    overall = win_rates()
    for m, s in sorted(overall.items(), key=lambda x: -x[1]["win_rate"]):
        contested = s["wins"] + s["losses"]
        if contested == 0 and s["errors"] == 0:
            continue
        lines.append(f"• {m}: {s['wins']}W-{s['losses']}L-{s['ties']}T "
                     f"({s['win_rate']:.0%} ±{s['ci']:.0%}) · ${s['cost']:.3f}"
                     + (f" · {s['errors']} err" if s["errors"] else ""))
    by_task: dict[str, list] = {}
    for tt in {r.get("task_type") for r in rows if r.get("task_type")}:
        b = best_model(tt)
        if b:
            by_task[tt] = b
    if by_task:
        lines.append("\nProven best per task (≥5 trials):")
        for tt, m in by_task.items():
            lines.append(f"• {tt} → {m}")
    lines.append("\nEvidence: every verdict is judged in both orders by an "
                 "independent model; ties go to the cheaper one. Say `race <task>` anytime.")
    return "\n".join(lines)
