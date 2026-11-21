"""The army pipeline — the owner's exact flow:

  chat/gchat → INTAKE (understand the ask) → ORCHESTRATOR (distribute + select
  agents) → SPECIALISTS (do the work, in parallel for reads) → REVIEW (check the
  work) → OBSERVER (health/quality gate) → back to CHAT → user.

Deterministic Python (this module) owns the flow; agents do the reasoning. Every
stage is bounded and logged, so a stuck/failed stage self-heals or escalates
rather than hanging. This is the "task distribution, agent selection, review,
report to orchestrator, recheck, send to chat agent, send to user" loop.

The Conductor is the orchestrator (safest — keeps Claude Code's sandbox as the
security floor); OpenClaw is only the mouth/ears; Claude Code is the hands.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

logger = logging.getLogger("pipeline")


# ------------------------------------------------------------------- 1. INTAKE

async def intake(text: str, conversation: str = "") -> dict:
    """Understand what the owner is actually asking. Returns a structured request the
    orchestrator can route. Cheap model; falls back to heuristics if it fails."""
    from brain import claude_runner
    prompt = (
        "You are the intake agent for the owner's autonomous army. Classify his "
        "message into a structured request so the orchestrator can route it. "
        "Be precise about intent and what 'done' means.\n\n"
        f"Recent conversation:\n{conversation[:1500]}\n\n"
        f"His message: {text}\n\n"
        'Return STRICT JSON: {"intent":"<one line: what he wants>",'
        '"domains":[<subset of: research, analysis, comms, ops, coding, review, '
        'planning, testing, general>],"needs_action":<true if it changes state '
        '(send/edit/deploy), false if read-only>,"acceptance":"<what a good '
        'result looks like>","urgency":"<low|normal|high>"}'
    )
    out = ""
    try:
        async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                                 allowed_tools=[], model=None, max_turns=0):
            if ev.type == "result":
                out = ev.content or ""
    except Exception as e:
        logger.warning("intake llm failed: %s", e)
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            req = json.loads(m.group(0))
            req.setdefault("intent", text[:120])
            req.setdefault("domains", [])
            return req
        except json.JSONDecodeError:
            pass
    # heuristic fallback
    from conductor import orchestrator
    return {"intent": text[:120], "domains": orchestrator.classify(text),
            "needs_action": False, "acceptance": "", "urgency": "normal"}


# ------------------------------------------------------------- 2. REVIEW stage

async def review(request: dict, result: str) -> dict:
    """A reviewer checks the work against the acceptance criteria BEFORE it goes
    back to the owner. Returns {ok, verdict, notes}. Independent-eyes principle."""
    if not result or len(result) < 20:
        return {"ok": False, "verdict": "empty", "notes": "no substantive result"}
    from brain import claude_runner
    prompt = (
        "You are the review agent. Check this work against what the owner asked for. "
        "Be strict but fair — you're the last gate before he sees it.\n\n"
        f"He asked: {request.get('intent','')}\n"
        f"Good result looks like: {request.get('acceptance','(use judgment)')}\n\n"
        f"The work produced:\n{result[:3000]}\n\n"
        'Return STRICT JSON: {"ok":<true if it genuinely answers/does what he '
        'asked>,"verdict":"<pass|thin|wrong>","notes":"<one line: what to fix if '
        'not ok, else empty>"}'
    )
    out = ""
    try:
        async for ev in claude_runner.run_claude(prompt, agent="review-bot", tier="read",
                                                 allowed_tools=[], model=None, max_turns=0):
            if ev.type == "result":
                out = ev.content or ""
    except Exception as e:
        logger.warning("review failed (passing through): %s", e)
        return {"ok": True, "verdict": "unreviewed", "notes": ""}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"ok": True, "verdict": "unreviewed", "notes": ""}


# --------------------------------------------------------------- 3. FULL RUN

async def run(text: str, channel: str = "telegram", conversation: str = "") -> dict:
    """The full pipeline for one user ask. Returns {answer, request, observed}."""
    from conductor import orchestrator, blackboard, observer
    from conductor.loop import log_work
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()

    req = await intake(text, conversation)
    logger.info("[%s] intake: %s | domains=%s", task_id, req.get("intent","")[:60], req.get("domains"))
    blackboard.post("intake", "decision", f"Routing '{req.get('intent','')[:80]}' "
                    f"to {req.get('domains') or 'generalist'}")

    # ORCHESTRATE: distribute + select agents (orchestrator applies read-vs-write)
    # — now WITH the conversation, so specialists resolve follow-ups (context fix).
    result = await orchestrator.handle(text, channel=channel, conversation=conversation)

    # REVIEW: check the work before it reaches the owner
    rev = await review(req, result)
    if not rev.get("ok") and rev.get("verdict") in ("thin", "wrong"):
        # one recheck: hand the reviewer's note back for a corrected pass
        logger.info("[%s] review flagged '%s' — one recheck", task_id, rev.get("verdict"))
        blackboard.post("review", "blocker", f"{rev.get('verdict')}: {rev.get('notes','')[:100]}")
        retry = await orchestrator.handle(
            f"{text}\n\n(Reviewer feedback to address: {rev.get('notes','')})", channel=channel)
        rev2 = await review(req, retry)
        if rev2.get("ok") or len(retry) > len(result):
            result, rev = retry, rev2

    # OBSERVER: record health/quality for this run
    observed = observer.record_run(task_id, req, result, rev,
                                   elapsed=time.monotonic() - t0)
    log_work("pipeline", f"{req.get('intent','')[:70]} [{rev.get('verdict','?')}]",
             "done" if rev.get("ok") else "flagged",
             {"task_id": task_id, "domains": req.get("domains")})
    return {"answer": result, "request": req, "review": rev, "observed": observed}
