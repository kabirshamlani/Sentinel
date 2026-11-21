"""Root Cause Analysis -- the self-learning path for "you made a mistake".

When the user corrects Sentinel, we don't just apologize; we run a small RCA
that feeds BOTH long-term memory systems:

1. Skillbook  -- via learning_loop.record_feedback(feedback_type="correction"),
   which creates a HIGH-priority never-repeat rule scoped to the agent.
2. Error journal -- via error_journal.log_error, with a signal trace of what
   was misread and what should have happened. Once the same pattern recurs
   3+ times it auto-promotes to a Skillbook rule (see skillbook.check_and_promote).

This module is intentionally synchronous: every learning primitive it calls is
sync, and it must be safe to call inline from a correction handler.

Inspired by the Reflexion loop (attempt -> evaluate -> reflect) applied to
user corrections rather than tool failures: the human IS the evaluator.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

if __package__ in (None, ""):  # run directly as a script -> put repo root on path
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from learning import error_journal, learning_loop

logger = logging.getLogger("rca")

# Correction phrases that a user message is a correction (Sentinel got it wrong).
# These are anchored/multi-word so incidental words (a bare "don't" in "don't
# forget", or "revert" in "revert the migration please") do NOT falsely trigger.
CORRECTION_MARKERS = (
    "you made a mistake",
    "that's wrong",
    "thats wrong",
    "that was wrong",
    "that's not right",
    "thats not right",
    "that's incorrect",
    "thats incorrect",
    "you shouldn't have",
    "you shouldnt have",
    "why did you",
    "why would you",
    "stop doing that",
    "don't do that",
    "dont do that",
    "that's not what i",
    "thats not what i",
    "that is not what i",
    "undo that",
    "roll that back",
    "you messed up",
    "bad call",
)


def detect_correction(text: str) -> bool:
    """True if a message looks like the user correcting a mistake.

    Matches specific correction phrases (multi-word / anchored) rather than
    bare keywords, so 'don't forget to test' or 'revert the migration please'
    do not falsely trigger while genuine corrections still do.
    """
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in CORRECTION_MARKERS)


def open_rca(offending: dict, user_note: str, agent: str = "conductor") -> dict:
    """Run root-cause analysis for a single offending action.

    offending: a work-log-style entry describing the action Sentinel took.
        Expected (all optional) keys: "summary"/"description", "result",
        "detail", "session_id", "kind".
    user_note: the human's correction text -- this becomes the never-repeat
        rule and the "what should have happened".
    agent: which agent to scope the learned rule to.

    Steps:
      1. record_feedback(feedback_type="correction", ...) -> HIGH-priority
         skillbook rule so the agent never repeats it.
      2. log_error(...) with signal_trace = what was misread (derived from the
         offending action) and what_should_have_happened = user_note.
      3. return {rule_added, error_logged, rca_summary}.
    """
    description = _describe_offending(offending)
    session_id = str(offending.get("session_id") or f"rca-{_stamp()}")
    task_summary = (
        offending.get("summary")
        or offending.get("description")
        or offending.get("kind")
        or description
    )

    # 1) Skillbook rule via the learning loop (creates a HIGH-priority
    #    correction rule from feedback_detail).
    feedback_entry = learning_loop.record_feedback(
        session_id=session_id,
        task_prompt=str(task_summary)[:300],
        feedback_type="correction",
        feedback_detail=user_note,
        agent_name=agent,
    )
    rule_added = bool(user_note)  # record_feedback only makes a rule when detail present

    # 2) Error journal entry. Classify the correction text; corrections are
    #    almost always reasoning errors, so fall back to that rather than the
    #    generic "unknown".
    error_type = error_journal.classify_error(user_note)
    if error_type == "unknown":
        error_type = "reasoning_error"

    signal_trace = _derive_signal_trace(offending)

    journal_entry = error_journal.log_error(
        agent=agent,
        session_id=session_id,
        task_summary=str(task_summary)[:200],
        error_type=error_type,
        error_message=f"User correction: {user_note}"[:500],
        signal_trace=signal_trace,
        what_should_have_happened=user_note,
        pattern_name=f"user-correction-{agent}",
    )

    rca_summary = (
        f"RCA for {agent}: misread '{signal_trace[:80]}'. "
        f"User says: {user_note[:120]}. "
        f"Logged as {error_type}; HIGH-priority rule "
        f"{'created' if rule_added else 'skipped (no note)'}."
    )

    logger.info("Opened RCA for agent=%s error_type=%s", agent, error_type)

    return {
        "rule_added": rule_added,
        "error_logged": True,
        "rca_summary": rca_summary,
        "feedback": feedback_entry,
        "journal_entry": journal_entry,
    }


def handle_inbound_correction(text: str, recent_work_fn) -> dict:
    """Handle an inbound correction message end to end.

    text: the user's message (assumed to be, or suspected to be, a correction).
    recent_work_fn: a zero-arg callable returning a list of recent work-log
        entries (most recent last, matching conductor.loop.recent_work()).

    Picks the most recent offending action, runs open_rca on it, and -- if a
    reversible compensating action is obvious -- attaches a description of it
    under "compensating_action" so the CALLER can broker.propose it as
    critical/self_modify. This function never executes the compensating action.
    """
    if not detect_correction(text):
        return {"status": "not_a_correction", "handled": False}

    try:
        entries = recent_work_fn() or []
    except Exception as e:  # a broken work-log must not crash the RCA path
        logger.warning("recent_work_fn failed: %s", e)
        entries = []

    offending = _pick_offending(entries)
    if offending is None:
        # Nothing to point at, but still capture the lesson against the
        # default agent so the correction is not lost.
        result = open_rca({"summary": "unattributed correction"}, text)
        result["status"] = "no_offending_action_found"
        result["handled"] = True
        return result

    agent = str(offending.get("agent") or offending.get("kind") or "conductor")
    result = open_rca(offending, text, agent=agent)
    result["status"] = "rca_opened"
    result["handled"] = True
    result["offending"] = offending

    compensating = _suggest_compensating_action(offending)
    if compensating is not None:
        result["compensating_action"] = compensating

    return result


# ---------- helpers ----------

# Work "kinds" that produced a side effect we can undo with an obvious inverse.
# Maps the original action-type to a reversible compensating action-type that
# the caller can hand to broker.propose (action_type, criticality "critical").
_REVERSIBLE_INVERSES: dict[str, dict] = {
    "send_gchat": {"action_type": "delete_gchat", "verb": "delete the message I sent"},
    "draft_gchat": {"action_type": "discard_draft", "verb": "discard the draft"},
    "draft_email": {"action_type": "discard_draft", "verb": "discard the email draft"},
    "schedule_meeting": {"action_type": "cancel_meeting", "verb": "cancel the meeting I scheduled"},
    "create_branch": {"action_type": "delete_branch", "verb": "delete the branch I created"},
    "create_pr": {"action_type": "close_pr", "verb": "close the PR I opened"},
    "self_modify": {"action_type": "revert_self_modify", "verb": "revert the self-modification"},
}


def _suggest_compensating_action(offending: dict) -> dict | None:
    """If the offending action has an obvious reversible inverse, describe it.

    Returns a dict suitable to pass to broker.propose (action_type/description/
    details/reasoning/reversibility). Does NOT execute anything.
    """
    kind = str(offending.get("kind") or offending.get("type") or offending.get("action_type") or "")
    inverse = _REVERSIBLE_INVERSES.get(kind)
    if inverse is None:
        return None

    description = _describe_offending(offending)
    return {
        "action_type": inverse["action_type"],
        "description": f"Compensate: {inverse['verb']} (undo of: {description[:120]})",
        "details": {
            "undo_of": kind,
            "original": offending.get("detail") or offending.get("result") or {},
        },
        "reasoning": "User indicated the original action was a mistake; propose the reversible inverse.",
        "reversibility": "reversible",
        "criticality_hint": "critical",
    }


def _pick_offending(entries: list[dict]) -> dict | None:
    """Pick the most recent action that actually did something worth undoing.

    Prefers the most recent entry whose kind matches a known reversible action;
    otherwise falls back to the most recent entry overall.
    """
    if not entries:
        return None

    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or entry.get("type") or entry.get("action_type") or "")
        if kind in _REVERSIBLE_INVERSES:
            return entry

    # Fall back to the most recent dict entry.
    for entry in reversed(entries):
        if isinstance(entry, dict):
            return entry
    return None


def _describe_offending(offending: dict) -> str:
    """Human-readable one-liner for the offending action."""
    for key in ("summary", "description", "kind", "type", "action_type"):
        val = offending.get(key)
        if val:
            return str(val)
    return "unknown action"


def _derive_signal_trace(offending: dict) -> str:
    """Derive 'what was misread' from the offending action description/result."""
    parts = []
    desc = _describe_offending(offending)
    if desc and desc != "unknown action":
        parts.append(desc)

    result = offending.get("result")
    if result is None:
        result = offending.get("detail")
    if result:
        parts.append(f"result: {str(result)[:150]}")

    return " | ".join(parts) if parts else "no signal captured from offending action"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


if __name__ == "__main__":
    # Guard against the old over-broad matching (bare 'don't'/'undo'/'revert').
    assert detect_correction("don't forget to test") is False
    assert detect_correction("you made a mistake") is True
    assert detect_correction("revert the migration please") is False
