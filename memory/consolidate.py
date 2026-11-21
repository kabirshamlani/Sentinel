"""Nightly memory consolidation — the "dream routine" (openclaw pattern).

Runs at ~03:30 daily from the daemon:
  1. Deterministic semantic maintenance (dedupe, decay-to-archive, caps) — code,
     never LLM, so contradictions resolve predictably (newest + user-source wins).
  2. DREAM: rewrite MEMORY.md (what happened + what matters) within budget.
  3. BRAINLIFT: update BRAINLIFT.md — DOK hierarchy with the two-gate filter
     (high importance + non-consensus). Replace, don't append.
  4. Trim conversation logs.
  5. Sunday: distill work insights.

Corrections are exempt from decay — a repeated mistake is a system failure.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from config.paths import MEMORY_ROOT

logger = logging.getLogger("consolidate")

MEMORY_MD = MEMORY_ROOT / "MEMORY.md"
BRAINLIFT_MD = MEMORY_ROOT / "BRAINLIFT.md"
MEMORY_BUDGET_CHARS = 2500

_SEED_MEMORY = """# Sentinel Memory

## Who I serve
- The owner of this machine and its connected accounts.

## Standing rules
- Sends (email/chat/PR/infra) ALWAYS go through approval. Reads are free.
- Never repeat a flagged mistake — corrections live in semantic memory.
- Low false positives: when in doubt, stay silent or put it in the digest.

## Active state
- (nothing yet)
"""

_SEED_BRAINLIFT = """# Sentinel knowledge base

## DOK1: Foundational Facts
(none yet)

## DOK2: Compressed Summaries
(none yet)

## DOK3: Non-Obvious Insights
(none yet)

## DOK4: Spiky Points of View
(none yet)

---
Items: DOK1: 0/15 | DOK2: 0/10 | DOK3: 0/5 | DOK4: 0/3
"""


def ensure_seeds() -> None:
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    if not MEMORY_MD.exists():
        MEMORY_MD.write_text(_SEED_MEMORY, encoding="utf-8")
    if not BRAINLIFT_MD.exists():
        BRAINLIFT_MD.write_text(_SEED_BRAINLIFT, encoding="utf-8")


def memory_block() -> str:
    ensure_seeds()
    try:
        return MEMORY_MD.read_text(encoding="utf-8")[:MEMORY_BUDGET_CHARS]
    except OSError:
        return ""


# ------------------------------------------------- 1. deterministic upkeep

def semantic_maintenance() -> dict:
    """ONE curation path now — delegate to the unified gateway's consolidation
    (decay unused non-sacred rows to archive + cap the active set). This replaces
    the old facts-only maintenance so there's no double bookkeeping."""
    from memory import gateway
    return gateway.consolidate(max_active=400, decay_days=90)


# --------------------------------------------------------- 2 & 3. LLM passes

async def _run_llm(prompt: str) -> str:
    from brain import claude_runner
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             allowed_tools=[], model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    return out.strip()


async def dream() -> bool:
    """Consolidate MEMORY.md via DELTA OPS, not a wholesale rewrite.

    ACE (research doc 11 §a#2): iterative full rewrites erode knowledge ('context
    collapse') via brevity bias. So the LLM emits bullet-level operations
    (add/edit/remove) which Python applies to the existing file — detail is
    preserved across many nights instead of being re-summarized away."""
    ensure_seeds()
    import json as _json
    from conductor.loop import recent_work
    from channels.conversation import Conversation
    work = recent_work(hours=26) or []
    work_lines = "\n".join(f"- {w.get('vertical','')}: {w.get('summary', w.get('task',''))} "
                           f"[{w.get('status','')}]" for w in work[:30]) or "(no logged work)"
    convo = Conversation("telegram").transcript(30) or "(no conversation)"
    current = MEMORY_MD.read_text(encoding="utf-8")
    prompt = (
        "Nightly memory consolidation for Sentinel, the owner's autonomous agent. "
        "Do NOT rewrite MEMORY.md — that erodes knowledge over time. Instead emit "
        "a small set of DELTA OPERATIONS that improve it.\n\n"
        "Allowed ops (JSON list):\n"
        '  {"op":"add","section":"Active state","text":"<new bullet>"}\n'
        '  {"op":"edit","match":"<substring of an existing bullet>","text":"<replacement>"}\n'
        '  {"op":"remove","match":"<substring of a stale/resolved bullet>"}\n\n'
        "RULES: never remove standing rules/boundaries/who-I-serve. Add only "
        "significant new state/decisions/learnings from today. Remove only "
        "resolved/stale/duplicated items. Be surgical — 0 to ~6 ops is normal; an "
        "empty list is fine on a quiet day.\n\n"
        f"## Current MEMORY.md\n{current[:3000]}\n\n## Today's work\n{work_lines}\n\n"
        f"## Today's conversation\n{convo[:2000]}\n\n"
        "Return ONLY a JSON list of ops."
    )
    out = await _run_llm(prompt)
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return False
    try:
        ops = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return False
    changed = _apply_delta_ops(current, ops)
    if changed is not None:
        MEMORY_MD.write_text(changed, encoding="utf-8")
        return True
    return False


def _apply_delta_ops(current: str, ops: list) -> str | None:
    """Apply add/edit/remove ops to the markdown, preserving structure."""
    if not isinstance(ops, list):
        return None
    lines = current.split("\n")
    applied = 0
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "remove" and op.get("match"):
            match = op["match"]
            new_lines = [l for l in lines if match not in l]
            if len(new_lines) != len(lines):
                lines = new_lines
                applied += 1
        elif kind == "edit" and op.get("match") and op.get("text"):
            for i, l in enumerate(lines):
                if op["match"] in l and l.strip().startswith(("-", "*")):
                    indent = l[:len(l) - len(l.lstrip())]
                    lines[i] = f"{indent}- {op['text'].lstrip('- ')}"
                    applied += 1
                    break
        elif kind == "add" and op.get("text"):
            section = op.get("section", "Active state")
            new = f"- {op['text'].lstrip('- ')}"
            # insert under the named section header, else append
            placed = False
            for i, l in enumerate(lines):
                if l.strip().lower().startswith("##") and section.lower() in l.lower():
                    lines.insert(i + 1, new)
                    placed = True
                    applied += 1
                    break
            if not placed:
                lines.append(new)
                applied += 1
    if applied == 0:
        return None
    result = "\n".join(lines)
    # if the file grows past budget, that's the ONLY time we let the LLM condense —
    # and even then it's a one-shot compaction, not the default path.
    return result


async def brainlift() -> bool:
    """Update BRAINLIFT.md — DOK hierarchy, two-gate filter, replace-don't-append."""
    ensure_seeds()
    from conductor.loop import recent_work
    work = recent_work(hours=26) or []
    daily = "\n".join(f"- {w.get('vertical','')}: {w.get('summary', w.get('task',''))} "
                      f"[{w.get('status','')}]" for w in work[:30]) or "(quiet day)"
    current = BRAINLIFT_MD.read_text(encoding="utf-8")
    prompt = (
        "Nightly knowledge base routine for Sentinel (the owner's autonomous agent army). "
        "knowledge base is COMPRESSION, not storage: a small, high-signal artifact of "
        "what Sentinel knows that materially changes decisions.\n\n"
        "DOK hierarchy with HARD CAPS: DOK1 foundational facts (max 15, one-line "
        "assertions), DOK2 compressed summaries (max 10, 2–3 sentences of real "
        "substance), DOK3 non-obvious insights (max 5, insight+evidence+implication), "
        "DOK4 spiky POVs (max 3, claim+evidence+why the default view is wrong).\n\n"
        "TWO-GATE FILTER — every item must pass BOTH: (1) high importance: without "
        "it, a smart agent makes a costly mistake; (2) non-consensus: a knowledgeable "
        "person would be surprised or push back. 'True but obvious' fails. Replace "
        "weaker items, never just append. An empty DOK4 beats a weak one. Timestamp "
        "additions (Added: YYYY-MM-DD). End with the item-count footer.\n\n"
        f"## Current BRAINLIFT.md\n{current[:3500]}\n\n## Today's activity\n{daily}\n\n"
        "If nothing passes both gates, return the current document unchanged. "
        "Return ONLY the complete BRAINLIFT.md content."
    )
    out = await _run_llm(prompt)
    if out and "DOK1" in out:
        BRAINLIFT_MD.write_text(out, encoding="utf-8")
        return True
    return False


# ------------------------------------------------------------------ 4 & main

def trim_conversations() -> None:
    from channels.conversation import Conversation
    for ch in ("telegram", "gchat", "whatsapp"):
        c = Conversation(ch)
        d = c._load()
        if len(d.get("turns", [])) > 40:
            d["turns"] = d["turns"][-40:]
            c._save(d)


async def nightly() -> dict:
    res = {"at": datetime.now(timezone.utc).isoformat()}
    try:
        res["semantic"] = semantic_maintenance()
    except Exception as e:
        res["semantic_error"] = str(e)
    try:
        from memory import reasoning_bank
        res["reasoning_bank_lessons"] = reasoning_bank.prune()  # current count (curation done by gateway.consolidate)
    except Exception as e:
        res["reasoning_bank_error"] = str(e)
    try:
        res["dream"] = await dream()
    except Exception as e:
        res["dream_error"] = str(e)
    try:
        res["brainlift"] = await brainlift()
    except Exception as e:
        res["brainlift_error"] = str(e)
    try:
        trim_conversations()
    except Exception:
        pass
    try:
        from verticals.proactive import decay_tuning
        decay_tuning()   # B5: relax proactive thresholds toward neutral nightly
    except Exception:
        pass
    if datetime.now().weekday() == 6:   # Sunday: distill insights
        try:
            from learning import insights
            res["insights"] = bool(await insights.distill())
        except Exception as e:
            res["insights_error"] = str(e)
    logger.info("nightly consolidation: %s", res)
    return res
