"""Enrich — every issue gets a researched, evidence-backed plan before a fix.

The owner (2026-07-29): "issues are not well listed — no detailed plan with
evidence why this needs to be fixed and how; use the research agent before
filing; distribute tasks to the relevant agents." This module is that stage,
and it staffs the army properly:

  analyst    → code-level root-cause dossier + fix plan (every issue)
  researcher → external best-practice context (prompt/architecture classes,
               capped — web lookups cost time and money)
  planner    → ordered execution plan with owners for the multi-step
               instrumentation/architecture issues (the planner agent's first
               real job since it was orphaned)

The plan lands on the kanban row, flows into the fixer's brief (better
fixes), and is synced into the GitHub issue body (broker-gated update_issue)
so the board reads like a real engineering backlog, not one-liners.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.paths import REPO_ROOT
from coaching import kanban

logger = logging.getLogger("coach.enrich")

MAX_AGENT_TURNS = 24
PLAN_CAP = 6000
ENRICH_AGENT_TIMEOUT = 300      # hard per-agent wall-clock cap (s); a hung
                                # research call must not freeze the fix cycle


async def _run(agent: str, prompt: str, max_turns: int = MAX_AGENT_TURNS) -> str:
    from brain.claude_runner import run_claude
    final = ""

    async def _drive():
        nonlocal final
        # permission_mode="default": read tier otherwise runs the CLI in plan
        # mode, which writes the dossier to a plan FILE and returns only a
        # summary — we need the full text inline (tools stay read-only anyway)
        async for ev in run_claude(prompt, agent=agent, tier="read",
                                   working_dir=str(REPO_ROOT), max_turns=max_turns,
                                   permission_mode="default",
                                   episode_source="coach-run"):
            if ev.type in ("assistant", "result") and ev.content:
                final = ev.content

    try:
        # HARD wall-clock cap: a web-research agent that hangs on a network
        # call must never freeze the whole fix cycle (a 51-min stall on a stuck
        # researcher was the real cause of the board not moving, 2026-07-31).
        # run_claude has an IDLE timeout but not a total one; this is the total.
        await asyncio.wait_for(_drive(), timeout=ENRICH_AGENT_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("enrich agent %s exceeded %ds — skipping this section",
                       agent, ENRICH_AGENT_TIMEOUT)
    except Exception as e:
        logger.warning("enrich agent %s failed: %s", agent, e)
    return final.strip()


def _brief(issue: dict) -> str:
    ev = "\n".join(f"- [{e.get('stream','')}] {e.get('evidence','')[:200]}"
                   for e in issue.get("evidence", [])[:5])
    return (f"ISSUE {issue['id']} — {issue.get('title','')}\n"
            f"Agent under coaching: {issue.get('agent')} · failed check: {issue.get('check')} · "
            f"seen {issue.get('count',1)}x · severity {issue.get('severity')}\n"
            f"RCA class: {issue.get('rca_class')} — {issue.get('rca_why','')}\n"
            f"Fix channel: {issue.get('fix_channel')} → {issue.get('fix_target') or 'locate from evidence'}\n"
            f"Evidence from real runs:\n{ev}\n")


async def enrich_issue(issue: dict, broker=None, deep: bool = True) -> dict | None:
    """Produce the plan for one issue and persist it everywhere it's read."""
    parts: list[str] = []

    # 1) analyst: the code-grounded dossier (every issue)
    analyst_prompt = (
        _brief(issue) +
        "\nYou are enriching this issue into a proper engineering ticket. Read the "
        "ACTUAL code paths involved (cite file:line for every claim — no guesses). "
        "Produce EXACTLY these sections, total under 70 lines:\n"
        "## Why this matters\n(concrete impact on the owner/system, tied to the evidence)\n"
        "## Root cause\n(the mechanism, file:line)\n"
        "## Fix plan\n(numbered minimal steps, each naming the file to change)\n"
        "## Acceptance criteria\n(testable, binary — what proves it fixed)\n"
        "## Risks\n(what could regress, blast radius)")
    dossier = await _run("analyst", analyst_prompt)
    if dossier:
        parts.append(f"### Analysis (analyst)\n{dossier}")

    # 2) researcher: external best practice, only where outside knowledge helps
    if deep and issue.get("rca_class") in ("prompt-defect", "architecture-defect"):
        research_prompt = (
            f"Research how production LLM-agent systems handle this problem class:\n"
            f"{issue.get('title','')}\n{issue.get('rca_why','')}\n\n"
            "Search the web (2026 sources preferred). Return UNDER 15 lines: the 2-3 "
            "battle-tested patterns with one-line tradeoffs, each with a real source "
            "URL. If nothing genuinely applicable exists, say so in one line — never "
            "pad or fabricate sources.")
        research = await _run("researcher", research_prompt, max_turns=14)
        if research and "nothing genuinely applicable" not in research.lower():
            parts.append(f"### External patterns (researcher)\n{research}")

    # 3) planner: ordered execution plan for the multi-step classes
    if issue.get("fix_channel") == "instrumentation" or \
            issue.get("rca_class") == "architecture-defect":
        plan_prompt = (
            _brief(issue) +
            "\nBreak the fix into an ORDERED execution plan (under 12 steps). For each "
            "step: what changes, which file, which agent/tool of the Sentinel army owns "
            "it (code-bot writes code, review-bot reviews, the deterministic test gate "
            "verifies), and what evidence closes the step. Flag any step needing "
            "the owner's judgment. No prose padding — just the numbered plan.")
        steps = await _run("planner", plan_prompt, max_turns=16)
        if steps:
            parts.append(f"### Execution plan (planner)\n{steps}")

    if not parts:
        return None
    plan = {
        "text": "\n\n".join(parts)[:PLAN_CAP],
        "by": "analyst" + ("+researcher" if len(parts) > 1 else ""),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    kanban.move(issue["id"], issue.get("status", "backlog"), plan=plan)

    # sync the enriched body onto the GitHub issue (broker-gated, pre-approved)
    gh = (kanban.get(issue["id"]) or {}).get("github") or {}
    if broker is not None and gh.get("number"):
        try:
            row = kanban.get(issue["id"]) or issue
            await broker.propose(
                action_type="update_issue",
                description=f"Enrich coaching issue #{gh['number']} with the researched plan",
                details={"owner": "your-org", "repo": "sentinel",
                         "number": gh["number"],
                         "new_body": _github_body(row)},
                reasoning="Issues must read as researched tickets: evidence, root cause, "
                          "fix plan, acceptance criteria.",
                project="sentinel-self")
        except Exception as e:
            logger.debug("github body sync failed: %s", e)
    logger.info("enriched %s (%d sections)", issue["id"], len(parts))
    return plan


def _github_body(issue: dict) -> str:
    ev = "\n".join(f"- `{e.get('stream','')}` {e.get('evidence','')[:160]}"
                   for e in issue.get("evidence", [])[:5])
    body = (f"**Coach-filed issue** `{issue['id']}` — RCA class **{issue.get('rca_class')}**\n\n"
            f"Agent: `{issue.get('agent')}` · check: `{issue.get('check')}` · "
            f"occurrences: {issue.get('count',1)} · severity: {issue.get('severity')}\n\n"
            f"Why this class: {issue.get('rca_why','')}\n\n"
            f"Evidence:\n{ev}\n")
    plan = issue.get("plan") or {}
    if plan.get("text"):
        body += f"\n---\n\n{plan['text']}\n"
    fx = issue.get("fix") or {}
    if fx.get("branch"):
        body += (f"\n---\n**Fix:** branch `{fx.get('branch')}` commit `{fx.get('commit','')}` — "
                 f"repro before FAIL → after PASS · regression "
                 f"{'clean' if (fx.get('regression') or {}).get('passed') else 'n/a'}\n")
    return body[:60000]


async def enrich_backlog(broker=None, max_issues: int = 5, deep: bool = True) -> dict:
    """Backfill plans for backlog issues that lack one — highest severity first."""
    sev = {"high": 0, "medium": 1, "low": 2}
    todo = [i for i in kanban.issues("backlog") if not (i.get("plan") or {}).get("text")]
    todo.sort(key=lambda i: (sev.get(i.get("severity"), 1), -int(i.get("count", 1))))
    done, failed = [], []
    for issue in todo[:max_issues]:
        plan = await enrich_issue(issue, broker, deep=deep)
        (done if plan else failed).append(issue["id"])
    return {"enriched": done, "failed": failed,
            "remaining_unplanned": max(0, len(todo) - len(done) - len(failed))}
