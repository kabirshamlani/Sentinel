"""Orchestrator — turns Sentinel from one generalist into a real army.

Research verdict (agent-army-research/11 §e): specialize by READ-vs-WRITE, not
by hype.
  * trivial / conversational        -> answer inline (no specialist, cheap)
  * one clear domain                -> route to ONE specialist
  * broad read/research/discovery   -> fan out specialists in PARALLEL, synthesize
                                       (Anthropic orchestrator-worker: big quality
                                        win on read-heavy work)
  * anything with shared WRITE state -> a single continuous agent (coding stays
                                       one agent per worktree — fan-out loses
                                       context, ~37% of MAS failures)

Deterministic Python decides the SHAPE (this module, no LLM); specialists do the
reasoning (Claude Code). Coordination is via the BLACKBOARD (shared memory pool
in conductor/kv.py), never message-passing handoffs.

Every specialist run carries: an explicit objective, the retrieved ReasoningBank
lessons for that task-type, and the shared blackboard context. Failures/wins are
distilled back into memory so the army gets better over time.
"""

from __future__ import annotations

import asyncio
import logging
import re

from channels.formatting import VOICE

logger = logging.getLogger("orchestrator")

# domain -> specialist agent slug (files in .claude/agents/)
SPECIALISTS = {
    "research": "researcher",
    "analysis": "analyst",
    "comms": "comms-drafter",
    "ops": "ops-watcher",
    "planning": "planner",
    "testing": "tester",
    "coding": "code-bot",
    "review": "review-bot",
    "extraction": "data-extractor",
    "synthesis": "synthesizer",
}

_DOMAIN_HINTS = {
    "research": r"\b(research|find out|look up|what.?s new|latest|compare|competitive|papers?|articles?|benchmark|state of the art)\b",
    "analysis": r"\b(analyze|analy[sz]e|why is|root ?cause|investigate|debug|understand (the|this|my) (code|repo|log|system)|explain (the|this|my)|dig into|look into)\b",
    "comms": r"\b(draft|reply|email|message|write to|respond|compose|meeting (notes?|prep)|post)\b",
    "ops": r"\b(ci|infra|aws|deploy|health|is anything (wrong|broken|on fire)|monitor|pipeline|outage)\b",
    "coding": r"\b(fix|implement|refactor|build|add (a|the) (feature|function|endpoint)|code|bug in)\b",
    "extraction": r"\b(extract|pull out|parse|get the (numbers?|data|action items?|list)|turn .* into (a )?(table|json|csv)|structure)\b",
}

# domains that are READ-only (safe to fan out in parallel)
_READ_DOMAINS = {"research", "analysis", "ops", "extraction"}


def classify(text: str) -> list[str]:
    """Return matched domains, most-specific first. Empty = trivial/conversational."""
    low = text.lower()
    hits = [d for d, pat in _DOMAIN_HINTS.items() if re.search(pat, low)]
    # de-dupe while keeping analysis/research priority for ambiguous asks
    order = ["ops", "comms", "coding", "extraction", "analysis", "research"]
    return sorted(set(hits), key=lambda d: order.index(d) if d in order else 99)


def is_trivial(text: str) -> bool:
    """Short, conversational, no domain — handle inline, don't spin an agent."""
    low = text.strip().lower()
    if len(low) < 12:
        return True
    if re.match(r"^(hi|hey|hello|thanks|thank you|ok|okay|cool|nice|yes|no|sup|yo)\b", low):
        return True
    return not classify(text)


async def _run_specialist(agent: str, objective: str, context: str = "",
                          task_type: str = "chat", tier: str = "read",
                          tools: list[str] | None = None, working_dir: str | None = None) -> str:
    """Invoke one specialist with objective + retrieved lessons + blackboard."""
    from brain import claude_runner
    from conductor import blackboard
    try:
        from memory import reasoning_bank
        # enriched = our lessons + Hermes FTS5 recall when it's enabled (isolated)
        lessons = await reasoning_bank.retrieve_enriched(objective, task_type=task_type, k=4)
    except Exception:
        lessons = ""
    board = blackboard.snapshot_text()
    prompt = (
        f"{VOICE}\n\n"
        f"{lessons}\n\n" if lessons else f"{VOICE}\n\n"
    ) + (
        f"Shared context (blackboard):\n{board}\n\n" if board else ""
    ) + (
        f"Background:\n{context}\n\n" if context else ""
    ) + f"Your objective:\n{objective}\n\nDo it now and report the result:"
    out = ""
    async for ev in claude_runner.run_claude(
            prompt, agent=agent, tier=tier, task_type=task_type,
            allowed_tools=tools, model=None, max_turns=0, working_dir=working_dir):
        if ev.type == "result":
            out = ev.content or ""
    # distill the outcome into memory (win/failure both -> retrievable lesson)
    try:
        from memory import reasoning_bank
        reasoning_bank.record(objective, agent=agent, task_type=task_type,
                              outcome="done" if out else "empty", result=out[:400])
    except Exception:
        pass
    return out


async def handle(objective: str, channel: str = "telegram", conversation: str = "") -> str:
    """Main entry for non-trivial work. Returns the synthesized answer.

    `conversation` is the recent transcript — threaded into EVERY specialist so
    follow-ups resolve (was the #1 context-loss bug: it reached only intake)."""
    domains = classify(objective)
    if not domains:
        return await _run_specialist("conductor", objective, task_type="chat",
                                     context=conversation)

    # single clear domain -> one specialist
    if len(domains) == 1:
        d = domains[0]
        return await _run_specialist(
            SPECIALISTS[d], objective, task_type=d, context=conversation,
            tier="read", tools=["Read", "Grep", "Glob", "Bash"] +
            (["WebSearch", "WebFetch"] if d == "research" else []))

    # multiple READ domains -> parallel fan-out + synthesis (orchestrator-worker)
    read_domains = [d for d in domains if d in _READ_DOMAINS]
    if len(read_domains) >= 2:
        logger.info("fan-out across %s for: %s", read_domains, objective[:60])
        results = await asyncio.gather(*[
            _run_specialist(SPECIALISTS[d],
                            f"(Focus on the {d} angle) {objective}",
                            task_type=d, tier="read", context=conversation,
                            tools=["Read", "Grep", "Glob", "Bash"] +
                            (["WebSearch", "WebFetch"] if d == "research" else []))
            for d in read_domains], return_exceptions=True)
        parts = [f"[{d}]\n{r}" for d, r in zip(read_domains, results)
                 if isinstance(r, str) and r]
        return await _synthesize(objective, parts)

    # mixed (has a write domain) -> route to the primary specialist only
    return await _run_specialist(
        SPECIALISTS[domains[0]], objective, task_type=domains[0], context=conversation,
        tier="read", tools=["Read", "Grep", "Glob", "Bash"])


async def _synthesize(objective: str, parts: list[str]) -> str:
    if not parts:
        return "I looked into it from a few angles but came up empty — try rephrasing?"
    if len(parts) == 1:
        return parts[0].split("]\n", 1)[-1]
    from brain import claude_runner
    prompt = (
        f"{VOICE}\n\nThe owner asked: {objective}\n\n"
        "Several specialists each investigated one angle. Synthesize their "
        "findings into ONE coherent answer for him — resolve any conflicts, drop "
        "redundancy, lead with the bottom line. Don't mention the specialists.\n\n"
        + "\n\n".join(parts) + "\n\nSynthesized answer:"
    )
    out = ""
    # use the dedicated synthesizer specialist (read-only) for the last mile
    async for ev in claude_runner.run_claude(prompt, agent="synthesizer", tier="read",
                                             allowed_tools=["Read"], model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    return out or "\n\n".join(p.split("]\n", 1)[-1] for p in parts)
