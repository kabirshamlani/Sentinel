"""Intelligent email triage — the fix for "90% is spam, stop drafting to Coursera".

Two-stage, from langchain's executive-ai-assistant (research doc 15):
  Stage 0 — deterministic pre-filter (NO LLM): kills ~60-70% of noise for free
    (noreply@, List-Unsubscribe/bulk, transcript-bots, marketing) → NOISE.
  Stage 1 — cheap LLM (Haiku) classifies each survivor into EXACTLY one of
    NO | NOTIFY | EMAIL, with the "would a human actually reply from their phone?"
    test and the NON-NEGOTIABLE notify-default: if unsure → NOTIFY, never draft.

Only EMAIL-bucket mail gets a drafted reply. NOTIFY is surfaced (not drafted). NO
is dropped. VIP senders are never dropped by the pre-filter. Corrections (sacred
in the gateway) are injected as few-shot examples so it learns the owner's calls.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger("triage")

# Stage-0 deterministic NOISE signals (never draft, usually don't even notify)
_NOISE_SENDER = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|notifications?@|mailer-daemon|"
    r"automated|@mailchimp|@substack|@sendgrid|@e\.read\.ai|read\.ai|otter\.ai|"
    r"fireflies|gemini-notes@|calendar-notification|@coursera|@m\.learn|"
    r"marketing@|newsletter@|updates@|digest@|@notify\.|support@.*kayako|"
    r"@fireworks\.ai|@x\.ai|grok@|@zearn|@n8n\.io|@trulyfoundry|@trueFoundry|"
    r"@google\.com.*ai-|provisioning@|@anthropic\.com.*noreply)", re.I)
_NOISE_SUBJECT = re.compile(
    r"(unsubscribe|receipt|invoice #|your (code|otp)|verify your|newsletter|"
    r"weekly digest|has invited you|shared a (document|file) with you|"
    r"meeting notes|transcript|recording is ready|out of office)", re.I)
# transcript/notes bots → at most NOTIFY, never a reply
_TRANSCRIPT_BOT = re.compile(r"(read\.ai|otter|fireflies|gemini-notes|granola)", re.I)


def prefilter(em: dict, vips: set[str] | None = None) -> str | None:
    """Return 'no' / 'notify' if a deterministic rule fires, else None (→ Stage 1).
    A VIP sender is never dropped — forced to at least NOTIFY."""
    frm = (em.get("from", "") or "").lower()
    subj = (em.get("subject", "") or "").lower()
    addr = _addr(frm)
    if vips and addr in vips:
        return None                       # let the LLM decide (could be EMAIL)
    if _TRANSCRIPT_BOT.search(frm):
        return "notify"                   # meeting recaps: surface, never reply
    if _NOISE_SENDER.search(frm) or _NOISE_SUBJECT.search(subj):
        return "no"
    return None


def _addr(s: str) -> str:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", s or "")
    return m.group(0).lower() if m else s


async def classify(em: dict, *, goals: str = "", vips: set[str] | None = None,
                   few_shot: str = "") -> dict:
    """Classify ONE email → {label: no|notify|email, reason}. Stage-0 first, then a
    cheap Haiku LLM with notify-default. Never raises; defaults to 'notify' on error
    (the safe bias — surface, don't auto-draft)."""
    pre = prefilter(em, vips)
    if pre:
        return {"label": pre, "reason": "deterministic pre-filter", "stage": 0}
    from brain import claude_runner
    from prompts import load
    prompt = load(
        "triage.classify",
        goals=(f"His priorities: {goals}\n" if goals else ""),
        few_shot=(f"Past calls (learn from these):\n{few_shot}\n" if few_shot else ""),
        sender=em.get("from", ""), subject=em.get("subject", ""),
        preview=(em.get("snippet") or em.get("body", ""))[:400])
    out = ""
    try:
        async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                                 task_type="triage_classify",
                                                 allowed_tools=[], model=None, max_turns=0):
            if ev.type == "result":
                out = ev.content or ""
    except Exception as e:
        logger.debug("triage classify failed: %s", e)
        return {"label": "notify", "reason": "classifier error → safe notify", "stage": 1}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            lbl = (d.get("label") or "notify").lower()
            if lbl not in ("no", "notify", "email"):
                lbl = "notify"
            return {"label": lbl, "reason": (d.get("reason") or "")[:120], "stage": 1}
        except json.JSONDecodeError:
            pass
    return {"label": "notify", "reason": "unparseable → safe notify", "stage": 1}


def load_vips() -> set[str]:
    """VIP addresses from the user model (contacts/goals) — never auto-dropped."""
    try:
        from verticals.day import load_vips as _v
        return _v()
    except Exception:
        return set()


def few_shot_from_corrections(k: int = 5) -> str:
    """Turn sacred triage corrections into few-shot examples so it learns the owner's
    calls (e.g. 'never reply to Coursera')."""
    try:
        from memory import gateway
        rows = [c for c in gateway.all_of("correction")
                if "triage" in c.get("text", "").lower() or "reply" in c.get("text", "").lower()]
        return "\n".join(f"- {c['text'][:120]}" for c in rows[:k])
    except Exception:
        return ""
