"""6 AM market briefing — what's new in AI/tech, with images, zero slop.

Pipeline (research doc 10): research via web-enabled agent → novelty filter
against everything already sent → og:image enrichment → Telegram album +
digest. Then two STRICTLY-GATED extras:
  * TI ENGG post candidate — only if a harsh judge scores ≥8/10 AND nothing
    similar was posted in the space recently AND weekly quota (2) allows.
    Posting itself is a CRITICAL approval — never automatic.
  * AI-watercooler engagement — max 2 suggestions/week, same bar.

Anti-slop: novelty beats prose. If the day is thin, say so in 3 items — never pad.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from conductor import kv

logger = logging.getLogger("market-brief")

WEEKLY_POST_QUOTA = 2
POST_SCORE_BAR = 8


async def _agent_json(prompt: str, tools: list[str] | None = None) -> list | dict | None:
    from brain import claude_runner
    out = ""
    async for ev in claude_runner.run_claude(
            prompt, agent="market_brief", tier="read",
            allowed_tools=tools if tools is not None else ["WebSearch", "WebFetch"],
            model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    m = re.search(r"\[.*\]|\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def gather_news() -> list[dict]:
    prompt = (
        "Research the most significant AI/tech developments of the LAST 24 HOURS "
        "for the owner — a senior engineer building autonomous agent systems "
        "(Claude Code, MCP, multi-agent orchestration, LLM APIs). Search broadly: "
        "model releases, agent tooling, MCP ecosystem, notable X/HN discussions, "
        "engineering blog posts, papers with practical impact.\n\n"
        "Pick the 5-8 items that genuinely matter. NO generic listicles, NO "
        "'exciting/game-changing' adjectives. For each item give why it matters to "
        "HIM specifically.\n\n"
        'Return STRICT JSON only: [{"title": "...", "url": "https://...", '
        '"why": "one line, specific to the owner", "source": "hn|x|blog|paper|release", '
        '"hot": 1-10}]'
    )
    items = await _agent_json(prompt)
    return items if isinstance(items, list) else []


def _novel(items: list[dict]) -> list[dict]:
    """Drop stories too similar to anything already sent (rolling 200 titles)."""
    seen: list[str] = kv.get("briefing_seen", []) or []
    seen_tokens = [set(t.lower().split()) for t in seen]
    fresh = []
    for it in items:
        toks = set((it.get("title") or "").lower().split())
        if not toks:
            continue
        dup = any(len(toks & s) / max(1, min(len(toks), len(s))) > 0.6 for s in seen_tokens)
        if not dup:
            fresh.append(it)
    kv.put("briefing_seen", (seen + [it.get("title", "") for it in fresh])[-200:])
    return fresh


async def _og_image(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SentinelBot)"})
        if r.status_code != 200:
            return None
        for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
            m = re.search(pat, r.text[:60_000], re.I)
            if m and m.group(1).startswith("http"):
                return m.group(1)
    except Exception:
        pass
    return None


async def run(router, telegram=None) -> dict:
    """The 6 AM run. Also callable on demand (chat: 'morning brief')."""
    items = _novel(await gather_news())
    if not items:
        await router.broadcast("🌅 Morning — quiet day in AI/tech, nothing that clears the bar. "
                               "(I only report what's genuinely new to you.)")
        return {"status": "thin"}
    items = sorted(items, key=lambda i: -(i.get("hot") or 0))[:8]

    # images for the top stories
    if telegram:
        media = []
        for it in items[:4]:
            img = await _og_image(it.get("url", ""))
            if img:
                media.append({"url": img, "caption": it.get("title", "")[:200]})
        if media:
            await telegram.send_media_group(media)

    lines = [f"🌅 **Morning brief** — {datetime.now().strftime('%a %d %b')} · {len(items)} items"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. **{it.get('title','')[:80]}**")
        lines.append(f"   {it.get('why','')[:120]} — {it.get('url','')}")
    await router.broadcast("\n".join(lines))

    posted = await _maybe_propose_ti_post(items)
    from conductor.loop import log_work
    log_work("briefing", f"morning brief: {len(items)} items", "done",
             {"proposed_post": posted})
    return {"status": "sent", "items": len(items), "proposed_post": posted}


# ---------------------------------------------- TI ENGG post (gated, critical)

def _post_quota_left() -> int:
    week = datetime.now().strftime("%G-W%V")
    q = kv.get("ti_post_quota", {}) or {}
    if q.get("week") != week:
        q = {"week": week, "used": 0}
        kv.put("ti_post_quota", q)
    return WEEKLY_POST_QUOTA - q.get("used", 0)


async def _find_space(name_fragment: str) -> dict | None:
    from tools import gog_workspace as gw
    if not gw.available():
        return None
    for s in await gw.list_spaces(60):
        disp = (s.get("displayName") or s.get("name") or "")
        if name_fragment.lower() in disp.lower():
            sid = s.get("space_id") or (s.get("name") if str(s.get("name", "")).startswith("spaces/") else disp)
            return {"id": sid, "display": disp}
    return None


async def _maybe_propose_ti_post(items: list[dict]) -> bool:
    """Harsh gate: is any item worth the owner posting to TI ENGG? ≥8/10 + dedupe + quota."""
    if _post_quota_left() <= 0 or not items:
        return False
    space = await _find_space("TI ENGG")
    recent_posts = ""
    if space:
        try:
            from tools import gog_workspace as gw
            msgs = await gw.read_space_messages(space["id"], 25)
            recent_posts = "\n".join((m.get("text") or "")[:100] for m in msgs if isinstance(m, dict))[:2000]
        except Exception:
            pass
    listing = "\n".join(f"{i+1}. {it.get('title','')} — {it.get('why','')} ({it.get('url','')})"
                        for i, it in enumerate(items[:6]))
    verdict = await _agent_json(
        "You are a harsh quality gate. The owner can post interesting finds to his org's "
        "TI ENGG space — but ONLY things a staff engineer would genuinely stop and read; "
        "his credibility depends on not posting noise. Recent posts in that space (do "
        f"not duplicate topics):\n{recent_posts or '(unknown)'}\n\n"
        f"Today's candidates:\n{listing}\n\n"
        "Score each 1-10 for post-worthiness (novelty to this audience, practical "
        "value, discussion potential). Most days NOTHING clears 8 — that's the "
        "expected outcome.\n"
        'Return STRICT JSON: {"best_index": <1-based or 0 if none>, "score": <1-10>, '
        '"draft": "the exact message the owner would post (1-3 sentences + link, his voice: '
        'direct, technical, no hype)"}', tools=[])
    if not isinstance(verdict, dict) or (verdict.get("score") or 0) < POST_SCORE_BAR \
            or not verdict.get("best_index"):
        return False
    idx = int(verdict["best_index"]) - 1
    if idx < 0 or idx >= len(items):
        return False
    draft = verdict.get("draft") or f"{items[idx].get('title')} — {items[idx].get('url')}"
    from approval.broker import ApprovalBroker
    broker = ApprovalBroker()
    from conductor.daemon import get_router
    router = get_router()
    if router:
        broker._deliver = router.deliver_approval
        broker._notify = router.notify_resolved
    await broker.propose(
        action_type="send_gchat",
        description=f"Post to TI ENGG: {items[idx].get('title','')[:60]}",
        details={"space": (space or {}).get("id", "TI ENGG"), "message": draft},
        reasoning=f"Scored {verdict.get('score')}/10 by the harsh gate; "
                  f"quota {_post_quota_left()}/{WEEKLY_POST_QUOTA} left this week.",
        project="ti-engg-posts", tier="A", reversibility="irreversible",
        criticality_hint="critical",
    )
    q = kv.get("ti_post_quota", {}) or {}
    q["used"] = q.get("used", 0) + 1
    kv.put("ti_post_quota", q)
    return True
