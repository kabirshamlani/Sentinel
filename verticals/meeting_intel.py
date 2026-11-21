"""Meeting Intel — pre-meeting prep notes + post-meeting action extraction.

Two read-only jobs, run each cycle:

  (a) PRE-MEETING  — for real meetings in the next few hours (ones with
      attendees or a Meet link), a Claude Code agent produces prep notes:
      agenda, things to discuss, things to demo, open questions — grounded in
      related recent emails / chats / transcripts where available.

  (b) POST-MEETING — for new meeting transcripts (Google Meet "Notes:"/
      transcript emails from the last day), an agent extracts action items, a
      short analysis, and "things to improve" suggestions.

Both are delivered as REPORTS, not approvals: this vertical is handed only the
`broker`, so it surfaces results by returning them in the result dict (the
conductor logs them and the reporter renders them) and, if the broker exposes a
plain notify path (`broker._notify`), it also pushes a heads-up there. It stays
read-only (tier="read"); it does not propose mutations by default.

All Google Workspace access is import-guarded and wrapped in try/except so a
missing tool layer or auth failure degrades gracefully instead of crashing.
"""

from __future__ import annotations

import logging

# workspace_tools is being ported in parallel; guard the import so this module
# always loads even if the tool layer (or Google auth) isn't present yet.
try:
    from tools import workspace_tools as ws
except Exception:  # pragma: no cover - import-time guard
    ws = None

from brain import claude_runner
from learning import learning_loop

logger = logging.getLogger("meeting-intel")

PRE_MEETING_HOURS = 6
POST_MEETING_DAYS = 1
MAX_PRE = 5
MAX_POST = 5


# ─── agent runner (read-only) ─────────────────────────────

async def _run_agent(prompt: str, session_id: str) -> tuple[str, str | None]:
    """Run the meeting-intel agent read-only; return (result_text, error)."""
    prompt = learning_loop.pre_task_injection("meeting_intel", prompt)
    result_text, err = "", None
    try:
        async for ev in claude_runner.run_claude(
            prompt, agent="meeting_intel", tier="read", model=None, max_turns=0,
        ):
            if ev.type == "result":
                # RCA 2026-08-14: a failed run's result IS the failure notice
                # (e.g. "Failed to authenticate ... 401"). Treating any
                # non-empty result as analysis stored raw 401 strings as
                # "meeting analysis" for 36h and logged success=True.
                if getattr(ev, "is_error", False):
                    err = (ev.content or "agent run failed").strip()[:300]
                    result_text = ""
                else:
                    result_text = ev.content or ""
            elif ev.type == "error":
                err = ev.content
    except Exception as e:
        err = str(e)

    success = err is None and bool(result_text)
    learning_loop.post_task_analysis(
        agent_name="meeting_intel", session_id=session_id,
        task_prompt=prompt[:200], result_text=result_text,
        success=success, error_message=err or "",
    )
    return result_text, err


# ─── (a) pre-meeting prep ─────────────────────────────────

def _is_real_meeting(ev: dict) -> bool:
    """A real meeting has other attendees or a Meet link (skip solo blocks)."""
    attendees = ev.get("attendees", []) or []
    others = [a for a in attendees if a.get("status") != "declined"]
    has_link = bool(ev.get("meet_link") or ev.get("hangout_link"))
    return has_link or len(others) >= 2


def _related_context(ev: dict) -> str:
    """Best-effort: pull recent emails/transcripts that mention the meeting."""
    if ws is None:
        return ""
    summary = ev.get("summary", "") or ""
    terms = [w for w in summary.split() if len(w) > 3][:4]
    context_parts: list[str] = []

    # recent transcripts (often reference recurring meetings by title)
    try:
        if hasattr(ws, "search_meeting_transcripts"):
            for t in (ws.search_meeting_transcripts(days=7) or [])[:3]:
                subj = t.get("subject", "")
                if terms and not any(term.lower() in subj.lower() for term in terms):
                    continue
                context_parts.append(f"[Recent transcript] {subj}\n{(t.get('body','') or t.get('snippet',''))[:1200]}")
    except Exception as e:
        logger.debug("meeting: transcript context failed: %s", e)

    # emails matching a meeting keyword
    try:
        if terms and hasattr(ws, "search_emails"):
            q = " OR ".join(f'"{t}"' for t in terms[:2])
            for em in (ws.search_emails(query=q, max_results=3) or []):
                context_parts.append(f"[Email] {em.get('from','')} · {em.get('subject','')}\n{em.get('snippet','')[:400]}")
    except Exception as e:
        logger.debug("meeting: email context failed: %s", e)

    return "\n\n".join(context_parts[:5])


async def _prep_for_event(ev: dict) -> dict | None:
    prompt_context = _related_context(ev)
    description = (ev.get("description", "") or "").strip()
    if not prompt_context and not description:
        # Nothing to ground prep notes in (no description, no related
        # emails/transcripts) — skip rather than ask the agent to produce
        # notes from thin air, which just yields a complaint about empty
        # input (mirrors _analyze_transcript's empty-body guard).
        logger.info("meeting: no prep produced for %s (no context)", ev.get("summary", ""))
        return None

    attendees = ", ".join(
        a.get("name") or a.get("email", "") for a in (ev.get("attendees", []) or [])
    )[:500]

    prompt = (
        "You are the owner's meeting prep assistant. Produce tight, useful prep notes for the "
        "meeting below. Be concrete and skimmable.\n\n"
        f"MEETING: {ev.get('summary','(no title)')}\n"
        f"WHEN: {ev.get('start','')}\n"
        f"ATTENDEES: {attendees or '(unknown)'}\n"
        f"DESCRIPTION: {(ev.get('description','') or '')[:1500]}\n"
    )
    if prompt_context:
        prompt += f"\nRELATED RECENT CONTEXT:\n{prompt_context}\n"
    prompt += (
        "\nWrite prep notes with these sections (use short bullets, omit a section only if truly N/A):\n"
        "- Agenda\n- Things to discuss\n- Things to demo\n- Open questions\n"
        "Ground everything in the context above; do not invent commitments or facts."
    )

    result_text, err = await _run_agent(prompt, session_id=f"prep-{ev.get('id','')}")
    if err or not result_text.strip():
        logger.info("meeting: no prep produced for %s (%s)", ev.get("summary",""), err or "empty")
        return None
    return {
        "event_id": ev.get("id", ""),
        "title": ev.get("summary", ""),
        "start": ev.get("start", ""),
        "meet_link": ev.get("meet_link", "") or ev.get("hangout_link", ""),
        "prep_notes": result_text.strip(),
    }


async def _run_pre_meeting() -> list[dict]:
    if ws is None:
        return []
    try:
        events = ws.get_upcoming_events(hours=PRE_MEETING_HOURS) or []
    except Exception as e:
        logger.warning("meeting: get_upcoming_events failed: %s", e)
        return []

    prep: list[dict] = []
    for ev in events:
        if not _is_real_meeting(ev):
            continue
        try:
            note = await _prep_for_event(ev)
        except Exception as e:
            logger.warning("meeting: prep failed for %s: %s", ev.get("summary", ""), e)
            continue
        if note:
            prep.append(note)
        if len(prep) >= MAX_PRE:
            break
    return prep


# ─── (b) post-meeting analysis ────────────────────────────

async def _analyze_transcript(t: dict) -> dict | None:
    body = t.get("body", "") or t.get("snippet", "")
    if not body.strip():
        return None

    prompt = (
        "You are analysing a meeting transcript / notes email for the owner. Extract signal only.\n\n"
        f"SUBJECT: {t.get('subject','')}\nDATE: {t.get('date','')}\n\n"
        f"TRANSCRIPT / NOTES:\n{body[:8000]}\n\n"
        "Produce three clearly labelled sections:\n"
        "1. ACTION ITEMS — a bullet per item, with owner if stated (do not invent owners).\n"
        "2. ANALYSIS — 2-4 sentences: what was decided and what's still open.\n"
        "3. THINGS TO IMPROVE — brief suggestions for running this kind of meeting better.\n"
        "Only use what's in the transcript; do not fabricate."
    )
    result_text, err = await _run_agent(prompt, session_id=f"post-{t.get('id','')}")
    if err or not result_text.strip():
        logger.info("meeting: no analysis produced for %s (%s)", t.get("subject",""), err or "empty")
        return None
    return {
        "transcript_id": t.get("id", ""),
        "subject": t.get("subject", ""),
        "date": t.get("date", ""),
        "message_link": t.get("message_link", ""),
        "analysis": result_text.strip(),
    }


async def _run_post_meeting() -> list[dict]:
    if ws is None:
        return []
    try:
        transcripts = ws.search_meeting_transcripts(days=POST_MEETING_DAYS) or []
    except Exception as e:
        logger.warning("meeting: search_meeting_transcripts failed: %s", e)
        return []

    post: list[dict] = []
    for t in transcripts[:MAX_POST]:
        try:
            analysis = await _analyze_transcript(t)
        except Exception as e:
            logger.warning("meeting: analysis failed for %s: %s", t.get("subject", ""), e)
            continue
        if analysis:
            post.append(analysis)
    return post


# ─── delivery + entrypoint ────────────────────────────────

async def _maybe_notify(broker, text: str) -> None:
    """Push a plain heads-up if the broker exposes a notify path; else no-op."""
    notify = getattr(broker, "_notify", None)
    if not notify:
        return
    try:
        import asyncio
        r = notify(text, None)
        if asyncio.iscoroutine(r):
            await r
    except Exception as e:
        logger.debug("meeting: notify failed: %s", e)


def _summary(prep: list[dict], post: list[dict]) -> str:
    bits = []
    if prep:
        bits.append(f"{len(prep)} upcoming meeting(s) prepped")
    if post:
        bits.append(f"{len(post)} transcript(s) analysed")
    return "; ".join(bits) if bits else "no meetings or transcripts to process"


async def run_once(broker) -> dict:
    """One meeting-intel cycle: pre-meeting prep + post-meeting analysis.

    Read-only. Returns {"summary", "prep", "post"}; the conductor/reporter
    surfaces the notes. Never crashes on Google errors.
    """
    if ws is None:
        return {"summary": "workspace_tools unavailable", "prep": [], "post": [],
                "error": "workspace_tools unavailable"}

    prep = await _run_pre_meeting()
    post = await _run_post_meeting()
    summary = _summary(prep, post)

    if prep or post:
        lines = ["*Meeting intel*"]
        for p in prep:
            lines.append(f"\n📅 Prep — {p.get('title','')} ({p.get('start','')})\n{p.get('prep_notes','')[:1500]}")
        for a in post:
            lines.append(f"\n📝 Recap — {a.get('subject','')}\n{a.get('analysis','')[:1500]}")
        await _maybe_notify(broker, "\n".join(lines))

    logger.info("meeting: %s", summary)
    return {"summary": summary, "prep": prep, "post": post}
