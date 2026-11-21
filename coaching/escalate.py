"""Live-failure escalation — "if the bot can't do a task, call the coach."

The owner's directive (2026-08-15): when any agent fails at a REAL task, the
system must not just log it — it must summon the repair loop: file a coaching
issue immediately (with the live evidence) and flag the fix cycle to run at
the next daemon tick instead of waiting for the daily rubric review. The
coach then does what it already knows how to do: repro test → fix branch →
regression + anti-gaming gates → PM auto-merge → daemon self-restart.

This module is intentionally tiny and NEVER raises — escalation is a
best-effort side effect of a failure path that is already unhappy.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger("coach.escalate")


def _error_class(error: str) -> str:
    """Stable-ish class token for dedup: strip volatile bits (paths, ids,
    numbers) and keep the error's shape."""
    e = (error or "unknown")[:200].lower()
    e = re.sub(r"/[^\s]+", "<path>", e)
    e = re.sub(r"\b\d+\b", "<n>", e)
    e = re.sub(r"[a-f0-9]{8,}", "<hex>", e)
    return e[:120]


def live_failure(agent: str, task: str, error: str, context: str = "") -> str | None:
    """File a high-priority coaching issue for a live task failure and flag
    the fix cycle to run ASAP. Returns the issue id (existing on dedup)."""
    try:
        from coaching import kanban
        from conductor import kv

        now = datetime.now(timezone.utc).isoformat()
        fp = f"live:{agent}:" + hashlib.sha1(
            _error_class(error).encode()).hexdigest()[:10]
        issue = {
            "id": "ci-" + hashlib.sha1(fp.encode()).hexdigest()[:8],
            "fingerprint": fp,
            "title": f"[{agent}] live failure: {task[:70]} — {error[:60]}",
            "agent": agent,
            "check": "live_failure",
            "rca_class": "tool-defect",
            "fix_channel": "code",
            "fix_target": "",
            "rca_why": ("a REAL user-facing task failed at runtime — highest-"
                        "signal evidence there is; fix the code path, don't "
                        "paper over with prompts"),
            "severity": "high",
            "count": 1,
            "episode_ids": [],
            "evidence": [{"episode_id": "", "episode_ts": now,
                          "evidence": f"task: {task[:300]}\nerror: {error[:400]}"
                                      + (f"\ncontext: {context[:300]}" if context else ""),
                          "stream": "live"}],
            "judge_notes": [],
            "created": now,
        }
        res = kanban.file_issue(issue)
        # summon the fixer at the NEXT daemon tick (not the 2h schedule)
        kv.put("coach_fix_asap", now)
        logger.info("live failure escalated: %s (%s)", issue["title"][:80],
                    res.get("status"))
        return res.get("id") or issue["id"]
    except Exception as e:                        # never hurt the failing path further
        logger.warning("escalation failed (non-fatal): %s", e)
        return None


def request_capability(title: str, spec: str, target: str = "",
                       requested_by: str = "owner") -> str | None:
    """File a NEW-CAPABILITY request into the same repair loop that fixes
    defects, and summon the fixer ASAP.

    This is the organ that lets "implement X in yourself" become real work
    instead of dying in analysis: the orchestrator (or any code path) routes a
    self-improvement ask here; the fixer picks it up on the FEATURE channel
    (acceptance-test-first: write a test that encodes `spec` and fails because
    the capability is absent → build it → same regression/anti-gaming/feature-
    impl/review gates → PM holds it for the owner → merge → daemon self-restart).

    `title`  short human label; `spec` the observable behavior to build (the
    acceptance test is written against THIS); `target` optional module hint.
    Never raises — a self-improvement path should not take down its caller.
    """
    try:
        from coaching import kanban
        from conductor import kv

        now = datetime.now(timezone.utc).isoformat()
        # fingerprint on the spec text so re-asking the same capability dedups,
        # but two genuinely different features never collide
        spec_class = re.sub(r"\s+", " ", (spec or title or "").strip().lower())[:200]
        fp = "feat:" + hashlib.sha1(spec_class.encode()).hexdigest()[:12]
        issue = {
            "id": "ci-" + hashlib.sha1(fp.encode()).hexdigest()[:8],
            "fingerprint": fp,
            "title": f"[capability] {title[:80]}",
            "agent": "sentinel",
            "check": "capability_request",
            "rca_class": "feature",
            "fix_channel": "feature",
            "fix_target": target[:200],
            "rca_why": (f"The owner asked Sentinel to build this capability into itself "
                        f"(via {requested_by}); it does not exist yet. Prove it with an "
                        f"acceptance test that fails now and passes once built."),
            "severity": "high",
            "count": 1,
            "episode_ids": [],
            "evidence": [{"episode_id": "", "episode_ts": now,
                          "evidence": f"requested capability: {title[:120]}\n"
                                      f"spec (build the acceptance test against this): {spec[:600]}"
                                      + (f"\nlikely target: {target[:200]}" if target else ""),
                          "stream": "request"}],
            "judge_notes": [],
            "created": now,
        }
        res = kanban.file_issue(issue)
        kv.put("coach_fix_asap", now)
        logger.info("capability requested: %s (%s)", issue["title"][:80],
                    res.get("status"))
        return res.get("id") or issue["id"]
    except Exception as e:
        logger.warning("capability request failed (non-fatal): %s", e)
        return None
