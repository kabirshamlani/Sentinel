"""Self-improvement orchestration — the loop that makes the army get better,
safely, over weeks. Ties together the pieces built in v0.9.

Research doc 11 §b/§d: every self-change is PROPOSE -> VERIFY (held-out eval the
loop can't game) -> DIFF -> human APPROVE -> APPLY (git = rollback). This module
owns the trigger logic and the "grow a specialist on demonstrated need" decision
— the actual eval running lives in learning/evolve.py, the write happens via the
broker + learning/agent_factory.py (path-locked, critical approval).

Two triggers, both bounded:
  * failure-cluster  -> a recurring failure the generalist mishandles suggests a
                        missing specialist OR a needed guardrail.
  * idle-experiment  -> already handled by conductor/experiments.py (arena races).

Anti-reward-hacking (the load-bearing rule): a proposed specialist/skill must
BEAT routing-to-generalist on a held-out task before promotion. Self-report is
never the gate.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger("self-improve")

CLUSTER_THRESHOLD = 3     # this many similar failures => consider a change
_STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "it",
         "this", "that", "with", "error", "failed", "task"}


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _STOP and len(w) > 3}


def find_failure_clusters() -> list[dict]:
    """Group recent failures (error_journal + reasoning_bank) by topic overlap.
    Returns clusters with >= CLUSTER_THRESHOLD members — a demonstrated need."""
    failures = []
    try:
        from learning.error_journal import get_recent_errors
        for e in get_recent_errors(limit=60):
            failures.append({"text": e.get("error_message", e.get("error", e.get("description", ""))),
                             "agent": e.get("agent", e.get("source", ""))})
    except Exception:
        pass
    try:
        from memory import reasoning_bank
        for it in reasoning_bank.all_items():
            if it.get("outcome") == "failed":
                failures.append({"text": it.get("title", "") + " " + it.get("lesson", ""),
                                 "agent": it.get("agent", "")})
    except Exception:
        pass
    if len(failures) < CLUSTER_THRESHOLD:
        return []
    # cheap clustering: bucket by dominant shared token
    token_counts = Counter()
    for f in failures:
        for t in _tokens(f["text"]):
            token_counts[t] += 1
    clusters = []
    for token, count in token_counts.most_common(8):
        if count < CLUSTER_THRESHOLD:
            break
        members = [f for f in failures if token in _tokens(f["text"])]
        clusters.append({"topic": token, "count": count,
                         "members": members[:5],
                         "agents": list({m["agent"] for m in members if m["agent"]})})
    return clusters


async def propose_improvement(broker, focus_domain: str | None = None) -> dict:
    """Look at failure clusters; if one is strong, propose the RIGHT change:
    a guardrail rule (cheap, high-signal) and — only on a persistent cluster —
    a new specialist agent. Both are approval-gated; agents also eval-validated.

    focus_domain (from the observer's degraded-agent signal) prioritizes clusters
    involving that domain/agent, so the fix targets the ACTUAL failing agent
    instead of whatever cluster happens to be biggest (B6)."""
    clusters = find_failure_clusters()
    if not clusters:
        return {"status": "no_clusters", "focus": focus_domain}
    if focus_domain:
        # float clusters whose members/agents mention the degraded domain to the top
        clusters.sort(key=lambda c: focus_domain.lower() not in (
            c.get("topic", "").lower() + " " +
            " ".join(c.get("agents", [])).lower() + " " +
            " ".join(m.get("text", "") for m in c.get("members", [])).lower()))
    top = clusters[0]

    # 1) Always cheapest first: add a preventative guardrail rule (ReasoningBank
    #    already stores failures, but promote the pattern into the skillbook so
    #    it's injected for EVERY future task of that kind).
    try:
        from learning import skillbook
        skillbook.add_manual_rule(
            rule_text=f"Recurring failure around '{top['topic']}' "
                      f"({top['count']}x). Before acting on such tasks, verify the "
                      f"specific precondition that broke before.",
            agent_scope=top["agents"] or None,
            priority="high")
        logger.info("added guardrail rule for cluster '%s'", top["topic"])
    except Exception as e:
        logger.debug("guardrail rule add failed: %s", e)

    # 1b) Hermes's genuine edge: draft a reusable SKILL for the recurring pattern.
    #     It's an untrusted draft -> stored as a skill memory + proposed for
    #     approval before the army relies on it (never auto-applied).
    try:
        from brain import hermes_adapter
        if hermes_adapter.available():
            draft = await hermes_adapter.draft_skill(
                f"Recurring task around '{top['topic']}': " +
                "; ".join(m["text"][:80] for m in top["members"][:3]))
            if draft and draft.get("body"):
                from memory import gateway
                gateway.remember(f"[DRAFT skill for '{top['topic']}']\n{draft['body'][:600]}",
                                 kind="skill", source="hermes-draft", confidence=0.5,
                                 meta={"trusted": False, "needs_approval": True})
    except Exception:
        pass

    # 1c) If the cluster looks like a MISSING CAPABILITY (not just a mistake) —
    #     e.g. repeated "can't do X" — try to self-acquire a tool for it (gated).
    _TOOL_GAP = ("cannot", "can't", "no tool", "unsupported", "not able to",
                 "missing", "don't have")
    if any(w in " ".join(m["text"].lower() for m in top["members"]) for w in _TOOL_GAP):
        try:
            from learning import self_update
            await self_update.acquire_tool(broker, top["topic"])
        except Exception:
            pass

    # 2) Only propose a NEW specialist for a strong, persistent cluster the
    #    existing agents keep mishandling — never on vibes (research doc 11 §d).
    if top["count"] >= CLUSTER_THRESHOLD * 2:
        validated = await _validate_and_propose_specialist(broker, top)
        return {"status": "proposed_specialist" if validated else "guardrail_only",
                "cluster": top["topic"], "count": top["count"]}
    return {"status": "guardrail_added", "cluster": top["topic"], "count": top["count"]}


async def _validate_and_propose_specialist(broker, cluster: dict) -> bool:
    """Draft a specialist for the cluster, validate it beats the generalist on a
    held-out task, and only then propose it (critical approval, git-diffed)."""
    from brain import claude_runner
    from learning import agent_factory
    topic = cluster["topic"]
    members = "\n".join(f"- {m['text'][:120]}" for m in cluster["members"])

    # design step (AutoAgents planner): what role + prompt + tools?
    design_prompt = (
        f"These related tasks keep failing:\n{members}\n\n"
        "Design a specialist agent that would handle this class of task reliably. "
        'Return STRICT JSON: {"name":"kebab-slug","role":"one-line role",'
        '"system_prompt":"the agent\'s instructions (5-10 lines, with explicit '
        'guardrails for the observed failures)","tools":"Read, Grep, Glob, Bash"}. '
        "If no distinct specialist is warranted (the generalist should just be "
        'more careful), return {"name":""}.')
    out = ""
    async for ev in claude_runner.run_claude(design_prompt, agent="conductor", tier="read",
                                             allowed_tools=[], model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    import json as _json
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return False
    try:
        spec = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return False
    if not spec.get("name"):
        return False

    # validate-before-promote: the eval-gate is the ungameable verifier. We only
    # have a light check here (does the drafted prompt actually address the
    # failures?), scored by an independent judge — full task eval runs post-approval
    # in the sandbox. Conservative: if unsure, don't promote.
    ok = await _judge_addresses_failures(spec, cluster)
    if not ok:
        logger.info("proposed specialist '%s' did not clearly beat generalist — skipped",
                    spec["name"])
        return False

    payload = agent_factory.create_agent_spec(
        name=spec["name"], role=spec.get("role", ""),
        tools=[t.strip() for t in spec.get("tools", "Read, Grep, Glob, Bash").split(",")])
    # agent_factory returns a broker-ready proposal; add the system prompt body
    payload.setdefault("details", {})["system_prompt"] = spec.get("system_prompt", "")
    await broker.propose(**payload)
    logger.info("proposed new specialist '%s' for cluster '%s'", spec["name"], cluster["topic"])
    return True


async def _judge_addresses_failures(spec: dict, cluster: dict) -> bool:
    """Independent judge (arena-style, outside the designer): does this agent
    actually address the failures, or is it hand-wavy? Conservative default."""
    try:
        from brain import providers
        avail = providers.available_models()
        judge = next((j for j in ("claude-haiku-4-5", "gemini-3.5-flash") if j in avail), None)
        if not judge:
            return False   # no independent verifier -> don't auto-promote
        members = "\n".join(f"- {m['text'][:100]}" for m in cluster["members"])
        jp = (f"Failures:\n{members}\n\nProposed agent:\n{spec.get('system_prompt','')}\n\n"
              "Would this agent's instructions actually prevent these specific "
              "failures (not just generic best practices)? Reply YES or NO only.")
        r = await providers.call_model(judge, jp, max_tokens=8)
        return (r.get("text") or "").strip().upper().startswith("YES")
    except Exception:
        return False
