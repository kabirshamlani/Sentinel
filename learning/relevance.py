"""Relevance filter — work vs personal, so Sentinel ignores the shit-talk.

The owner: "if there is any chat that is not relevant to the work, ignore that."
Spaces/senders get classified once (heuristics first, LLM only when unsure),
cached in state, and personal ones are excluded from triage, briefings and
proactive candidates. Manual override: "ignore space X" / "space X is work".
"""

from __future__ import annotations

import logging
import re

from conductor import kv

logger = logging.getLogger("relevance")

_WORK_HINTS = re.compile(
    r"\b(engg|eng|dev|team|proj|sprint|standup|deploy|prod|alpha|beta|"
    r"review|design|arch|incident|oncall|api|bug|release|watercooler|gsd)\b", re.I)
_PERSONAL_HINTS = re.compile(
    r"\b(family|friends|home|trip|party|cricket|football|movie|food|swiggy|zomato|"
    r"bhai|yaar|fun|memes?)\b", re.I)


def classify_space(display_name: str, sample_texts: list[str] | None = None) -> str:
    """-> 'work' | 'personal' | 'unknown' (cached)."""
    rmap = kv.get("relevance_map", {}) or {}
    key = display_name.strip().lower()
    if key in rmap:
        return rmap[key]
    label = "unknown"
    blob = display_name + " " + " ".join(sample_texts or [])[:500]
    if _WORK_HINTS.search(blob):
        label = "work"
    elif _PERSONAL_HINTS.search(blob):
        label = "personal"
    if label != "unknown":
        rmap[key] = label
        kv.put("relevance_map", rmap)
    return label


def mark(display_name: str, label: str) -> None:
    """Manual override from chat ('ignore space X' -> personal)."""
    rmap = kv.get("relevance_map", {}) or {}
    rmap[display_name.strip().lower()] = label
    kv.put("relevance_map", rmap)


def is_work(display_name: str, sample_texts: list[str] | None = None) -> bool:
    """Personal = excluded; unknown defaults to included (better to see it once
    and learn than to silently miss work)."""
    return classify_space(display_name, sample_texts) != "personal"
