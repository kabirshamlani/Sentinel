"""Work-insights notebook — Sentinel notices patterns in the owner's work and
collects them until they're worth sharing.

Every vertical can append_insight(); a weekly distill (from consolidate.py)
compresses raw observations into WORK-INSIGHTS.md — the shareable artifact
The owner asked for ("note it down somewhere, when it is in good shape I can share
it with people"). Quality bar: observations need evidence, the distilled doc
follows the two-gate filter (important + non-obvious).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from config.paths import SENTINEL_HOME

logger = logging.getLogger("insights")

INSIGHTS_DIR = SENTINEL_HOME / "insights"
RAW_PATH = INSIGHTS_DIR / "raw.jsonl"
DOC_PATH = INSIGHTS_DIR / "WORK-INSIGHTS.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_insight(text: str, category: str = "work", evidence: str = "",
                   source: str = "sentinel") -> None:
    """Log a raw observation. Cheap; distillation applies the quality bar."""
    INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"at": _now(), "text": (text or "").strip()[:500], "category": category,
           "evidence": (evidence or "")[:300], "source": source}
    if not rec["text"]:
        return
    with open(RAW_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def recent(days: int = 14) -> list[dict]:
    if not RAW_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out = []
    for line in RAW_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            if datetime.fromisoformat(rec["at"]).timestamp() >= cutoff:
                out.append(rec)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return out


def digest() -> str:
    """For the /insights command."""
    if DOC_PATH.exists():
        doc = DOC_PATH.read_text(encoding="utf-8").strip()
        if doc:
            return doc[:3500]
    raw = recent(14)
    if not raw:
        return ("📓 Nothing collected yet. Insights build up as I watch your work "
                "patterns — check back in a few days.")
    lines = [f"📓 **{len(raw)} raw observations** (last 14d) — weekly distill pending:"]
    for r in raw[-8:]:
        lines.append(f"• {r['text'][:110]}")
    return "\n".join(lines)


async def distill() -> str | None:
    """Weekly: compress raw observations into WORK-INSIGHTS.md (two-gate filter)."""
    raw = recent(21)
    if len(raw) < 5:
        return None
    from brain import claude_runner
    existing = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else "(empty)"
    obs = "\n".join(f"- [{r['category']}] {r['text']}" +
                    (f" (evidence: {r['evidence']})" if r.get("evidence") else "")
                    for r in raw[-60:])
    prompt = (
        "You maintain WORK-INSIGHTS.md — a distilled document of genuinely valuable "
        "insights about the owner's engineering work, good enough that he could share it "
        "with colleagues. Apply a two-gate filter to every candidate: (1) important — "
        "would change how someone works; (2) non-obvious — a knowledgeable person "
        "would NOT say 'obviously'. Max 12 insights total; replace weaker items rather "
        "than appending; each insight: one bold claim + one evidence line. No generic "
        "advice, no filler. If nothing new passes the gates, return the existing doc "
        "unchanged.\n\n"
        f"Existing doc:\n{existing[:3000]}\n\nRaw observations (last 21 days):\n{obs}\n\n"
        "Return ONLY the complete updated markdown document."
    )
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             allowed_tools=[], model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    if out.strip():
        INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)
        header = f"<!-- distilled {_now()} -->\n"
        DOC_PATH.write_text(header + out.strip() + "\n", encoding="utf-8")
        logger.info("insights distilled: %d chars", len(out))
        return out
    return None
