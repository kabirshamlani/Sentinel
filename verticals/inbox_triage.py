"""Inbox Triage — read unread email + Google Chat -> draft replies in the owner's voice.

The whole vertical is safe by construction: perception is read-only (unread
Gmail + Chat DMs/spaces), the reasoning runs a Claude Code agent to draft a
reply, and the ONLY side effect is creating a DRAFT for human review — nothing
is ever sent. Draft creation is still gated through the approval broker as a
Tier-A reversible action, so the human sees each proposed reply before it lands
even as a draft.

Google Workspace access is import-guarded end to end: if tools.workspace_tools
or Google auth is unavailable the vertical logs and returns cleanly, never
crashing the conductor cycle.
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

logger = logging.getLogger("inbox-triage")

# how many unread items of each kind to look at per cycle
MAX_EMAILS = 15
MAX_DM_PEOPLE = 8
MAX_SPACES = 8
# don't spend an agent turn on more than this many drafts per cycle
MAX_DRAFTS_PER_CYCLE = 3   # fewer, higher-signal drafts (the owner: "90% is noise")

VOICE_GUIDE = (
    "Write in the owner's voice: concise, warm, and direct. No corporate filler, "
    "no over-apologising, no long preambles. Get to the point in a friendly "
    "human tone. Match the length to the message — short questions get short "
    "answers. Never invent facts, commitments, dates, or numbers you weren't "
    "given; if something needs the owner's input, say so plainly rather than "
    "guessing."
)


# ─── perception (Tier R, read-only) ───────────────────────

def _perceive() -> dict:
    """Pull unread email + Chat DMs and spaces. All Google calls guarded."""
    if ws is None:
        return {"error": "workspace_tools unavailable", "emails": [], "dms": [], "spaces": []}

    perception: dict = {"emails": [], "dms": [], "spaces": [], "sent_examples": []}
    errors: list[str] = []

    # unread emails
    try:
        perception["emails"] = ws.search_emails(query="is:unread", max_results=MAX_EMAILS) or []
    except Exception as e:
        logger.warning("inbox: unread email fetch failed: %s", e)
        errors.append(f"email:{e}")

    # unread Google Chat DMs
    try:
        for sp in (ws.list_dm_spaces() or [])[:MAX_DM_PEOPLE]:
            person = sp.get("display_name", "")
            if not person or person == "(DM)":
                continue
            try:
                convos = ws.read_dm_messages(person_name=person, max_messages=15) or []
            except Exception as e:
                logger.debug("inbox: DM read failed for %s: %s", person, e)
                continue
            for convo in convos:
                if convo.get("error") or convo.get("is_social"):
                    continue
                msgs = convo.get("messages", [])
                if msgs:
                    sid = convo.get("space_id", sp.get("id", ""))
                    label = convo.get("person", person)
                    # resolve a real name for a raw space id via the people roster
                    if not label or label in ("(DM)", "") or label.startswith("spaces/"):
                        try:
                            from tools import people
                            label = people.label_for_space(sid, fallback=label or sid)
                        except Exception:
                            pass
                    perception["dms"].append({
                        "person": label,
                        "space_id": sid,
                        "messages": msgs,
                    })
    except Exception as e:
        logger.warning("inbox: DM spaces fetch failed: %s", e)
        errors.append(f"dm:{e}")

    # spaces (rooms)
    try:
        for sp in (ws.list_spaces() or [])[:MAX_SPACES]:
            space_id = sp.get("id", "")
            if not space_id:
                continue
            try:
                msgs = ws.read_space_messages(space_id=space_id, max_results=15) or []
            except Exception as e:
                logger.debug("inbox: space read failed for %s: %s", space_id, e)
                continue
            if msgs:
                perception["spaces"].append({
                    "display_name": sp.get("display_name", ""),
                    "space_id": space_id,
                    "messages": msgs,
                })
    except Exception as e:
        logger.warning("inbox: spaces fetch failed: %s", e)
        errors.append(f"space:{e}")

    # recent sent examples for voice matching (best-effort; skip on any failure)
    try:
        if hasattr(ws, "get_user_sent_emails"):
            sent = ws.get_user_sent_emails(hours=72) or []
            perception["sent_examples"] = [
                {"to": s.get("to", ""), "subject": s.get("subject", ""),
                 "body_preview": (s.get("body_preview", "") or "")[:600]}
                for s in sent[:4]
            ]
    except Exception as e:
        logger.debug("inbox: sent-example fetch failed: %s", e)

    if errors:
        perception["error"] = "; ".join(errors)
    return perception


# ─── selection (which items plausibly need a reply) ───────

async def _select_items(perception: dict) -> list[dict]:
    """Triage perception → only EMAIL-bucket mail becomes a draft candidate; NOTIFY
    is surfaced (not drafted); NO is dropped. Uses the 3-bucket classifier with the
    notify-default so 90%-noise inboxes stop generating spam drafts."""
    from verticals import triage
    items: list[dict] = []
    vips = triage.load_vips()
    few = triage.few_shot_from_corrections()
    goals = ""
    try:
        from verticals import day
        goals = day.goals_text()[:400]
    except Exception:
        pass
    notify_items: list[dict] = []
    for em in perception.get("emails", []):
        r = await triage.classify(em, goals=goals, vips=vips, few_shot=few)
        lbl = r.get("label")
        if lbl == "email":
            items.append({"kind": "email", "data": em, "reason": r.get("reason", "")})
        elif lbl == "notify":
            notify_items.append({"from": em.get("from", ""),
                                 "subject": em.get("subject", ""),
                                 "reason": r.get("reason", "")})
    # surface (don't draft) the NOTIFY bucket — the reporter/heartbeat name these
    perception["_notify_count"] = len(notify_items)
    perception["_notify_items"] = notify_items

    # persistent per-thread dedup: don't re-draft the same DM/space whose latest message
    # we've already drafted for (gap: an unread item the owner hasn't actioned was re-drafting
    # every 2h cycle forever). Keyed on the latest message id, 14-day memory.
    from conductor import kv
    drafted = kv.get("inbox_drafted", {}) or {}
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        from learning import relevance
    except Exception:
        relevance = None

    def _last_msg_id(msgs):
        return str((msgs[-1] or {}).get("id") or (msgs[-1] or {}).get("name") or
                   (msgs[-1] or {}).get("create_time") or (msgs[-1] or {}).get("text", "")[:40]) if msgs else ""

    def _from_owner(m):
        m = m or {}
        snd = str(m.get("sender", "")).strip().lower()
        if snd in ("you", "me") or snd.startswith("owner"):
            return True
        # raw-id fallback: the gchat bridge calibrates the owner's users/<id>
        ident = (kv.get("gchat_identity") or {}).get("user_id", "")
        return bool(ident) and ident in (m.get("sender_id", ""), m.get("sender", ""))

    def _is_work(name, texts):
        if relevance is None or not hasattr(relevance, "is_work"):
            return True
        try:
            return relevance.is_work(name, texts)
        except Exception:
            return True

    for dm in perception.get("dms", []):
        msgs = dm.get("messages", [])
        last = msgs[-1] if msgs else {}
        if not last or _from_owner(last):          # nothing new / the owner spoke last
            continue
        person = dm.get("person", "")
        key = f"dm:{dm.get('space_id','')}:{_last_msg_id(msgs)}"
        if key in drafted:                          # already drafted for this exact message
            continue
        # gap-7: DMs must pass a relevance gate (was ungated — any low-value DM got a draft)
        if not _is_work(person, [m.get("text", "") for m in msgs[-5:]]):
            continue
        drafted[key] = now_iso
        items.append({"kind": "gchat", "data": dm})

    for sp in perception.get("spaces", []):
        msgs = sp.get("messages", [])
        last = msgs[-1] if msgs else {}
        # gap-4: spaces were drafted with ZERO gating. Require: has messages, latest is
        # NOT from the owner, it's a work space, and we haven't drafted for this message yet.
        if not last or _from_owner(last):
            continue
        name = sp.get("display_name", "")
        key = f"space:{sp.get('space_id','')}:{_last_msg_id(msgs)}"
        if key in drafted:
            continue
        if not _is_work(name, [m.get("text", "") for m in msgs[-5:]]):
            continue
        drafted[key] = now_iso
        items.append({"kind": "gchat_space", "data": sp})

    # prune the dedup store to 14 days and persist
    cutoff = (datetime.now(timezone.utc)).timestamp() - 14 * 86400
    drafted = {k: v for k, v in drafted.items()
               if _iso_ts(v) >= cutoff}
    kv.put("inbox_drafted", drafted)

    return items[:MAX_DRAFTS_PER_CYCLE]


def _iso_ts(iso: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


# ─── drafting via a Claude Code agent ─────────────────────

async def _run_agent(prompt: str, session_id: str) -> tuple[str, str | None]:
    """Run the inbox_triage agent; return (result_text, error).

    SECURITY (Rule-of-Two, gap P1-6): the prompt contains untrusted email/chat
    content, so this session gets NO Bash/Write/Edit tools — it only emits the
    draft text. The broker's executor (a trusted, credentialed step) actually
    creates the Gmail/GChat draft. This removes the tool surface from any
    untrusted-content session entirely.
    """
    prompt = learning_loop.pre_task_injection("inbox_triage", prompt)
    result_text, err = "", None
    try:
        async for ev in claude_runner.run_claude(
            prompt, agent="inbox_triage", tier="read",
            allowed_tools=["Read"], model=None, max_turns=0,
        ):
            if ev.type == "result":
                result_text = ev.content or ""
            elif ev.type == "error":
                err = ev.content
    except Exception as e:  # never let a runner crash sink the cycle
        err = str(e)

    success = err is None and bool(result_text)
    learning_loop.post_task_analysis(
        agent_name="inbox_triage", session_id=session_id,
        task_prompt=prompt[:200], result_text=result_text,
        success=success, error_message=err or "",
    )
    return result_text, err


def _parse_reply(text: str) -> tuple[str, str]:
    """Split an agent draft into (subject, body).

    The agent is asked to lead with 'SUBJECT: ...' then a blank line then the
    body; fall back to the whole text as the body if that shape is absent.
    """
    subject, body = "", (text or "").strip()
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SUBJECT:"):
            subject = stripped[len("SUBJECT:"):].strip()
            body = body.split(line, 1)[1].strip()
            break
    return subject, body


def _voice_examples_block(perception: dict) -> str:
    examples = perception.get("sent_examples", [])
    if not examples:
        return ""
    parts = ["\nRecent emails the owner has sent (match this voice):"]
    for ex in examples:
        parts.append(f"- To {ex.get('to','')} · {ex.get('subject','')}\n  {ex.get('body_preview','')[:400]}")
    return "\n".join(parts) + "\n"


async def _draft_email_reply(em: dict, perception: dict) -> dict | None:
    """Ask the agent to draft an email reply; return an approval payload or None."""
    msg_id = em.get("id", "")
    body = ""
    if ws is not None:
        try:
            full = ws.read_email(msg_id)
            body = (full.get("body", "") or "")[:4000]
        except Exception as e:
            logger.debug("inbox: read_email failed for %s: %s", msg_id, e)
            body = em.get("snippet", "")
    to = em.get("from", "")
    orig_subject = em.get("subject", "")
    reply_subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

    from prompts import load
    prompt = load("inbox.draft_email", voice_guide=VOICE_GUIDE,
                  voice_examples=_voice_examples_block(perception),
                  sender=to, subject=orig_subject, body=body, reply_subject=reply_subject)
    result_text, err = await _run_agent(prompt, session_id=f"email-{msg_id}")
    if err or not result_text.strip() or result_text.strip().upper() == "SKIP":
        logger.info("inbox: no email draft produced for %s (%s)", msg_id, err or "empty/skip")
        return None

    subject, reply_body = _parse_reply(result_text)
    subject = subject or reply_subject
    if not reply_body:
        return None

    return {
        "action_type": "draft_email",
        "description": f"Draft reply to {to}: {orig_subject[:70]}",
        "details": {"to": to, "subject": subject, "body": reply_body},
        "reasoning": "Unread email that appears to need a reply; drafted a response in the owner's voice for review.",
        "project": "inbox",
        "tier": "A",
        "reversibility": "reversible",
        "best_effort_plan": "save as draft for your review",
    }


async def _draft_gchat_reply(dm: dict, perception: dict) -> dict | None:
    """Ask the agent to draft a Google Chat reply; return an approval payload or None."""
    space_id = dm.get("space_id", "")
    # Human label: DM person, or the space's display name; resolve raw ids via
    # the roster. Never propose with an empty/opaque target (those failed in
    # the executor and read as "Draft chat reply to " noise on Telegram).
    label = dm.get("person") or dm.get("display_name") or ""
    if not label or label.startswith("spaces/") or label == "(DM)":
        try:
            from tools import people
            label = people.label_for_space(space_id, fallback="")
        except Exception:
            label = ""
    if not label or label.startswith("spaces/"):
        # senders now carry real names — label the DM by the other participant
        for m in reversed(dm.get("messages", [])):
            snd = str(m.get("sender", ""))
            if snd and not snd.startswith("users/") and "owner" not in snd.lower():
                label = snd.split("<")[0].strip()
                break
    if not label or label.startswith("spaces/"):
        logger.info("inbox: skipping gchat draft for %s — no resolvable name", space_id or "?")
        return None
    is_space = bool(dm.get("display_name")) and not dm.get("person")
    where = f"in {label}" if is_space else f"to {label}"

    msgs = dm.get("messages", [])
    transcript = "\n".join(
        f"{m.get('sender','')}: {m.get('text','')}" for m in msgs[-12:] if m.get("text")
    )
    if not transcript.strip():
        return None

    from prompts import load
    prompt = load("inbox.draft_gchat", voice_guide=VOICE_GUIDE, person=label,
                  transcript=transcript)
    result_text, err = await _run_agent(prompt, session_id=f"gchat-{label}")
    if err or not result_text.strip() or result_text.strip().upper() == "SKIP":
        logger.info("inbox: no gchat draft produced for %s (%s)", label, err or "empty/skip")
        return None

    message = result_text.strip()
    return {
        "action_type": "draft_gchat",
        "description": f"Draft chat reply {where}",
        "details": {"person_name": label, "space_id": space_id, "message": message},
        "reasoning": "Open Google Chat thread that appears to need a reply; drafted a response in the owner's voice for review.",
        "project": "inbox",
        "tier": "A",
        "reversibility": "reversible",
        "best_effort_plan": "save as draft for your review",
    }


# ─── entrypoint ───────────────────────────────────────────

async def run_once(broker, propose: bool = True) -> dict:
    """One triage cycle: perceive unread items -> draft replies -> propose drafts.

    Read-only when propose=False (perceive + log only). Never sends anything;
    proposed actions create Gmail/Chat DRAFTS only, gated through the broker.
    """
    perception = _perceive()
    if ws is None or (perception.get("error") and not any(
        perception.get(k) for k in ("emails", "dms", "spaces")
    )):
        return {"count": 0, "error": perception.get("error", "workspace_tools unavailable")}

    items = await _select_items(perception)
    logger.info("inbox: %d reply candidates (emails=%d dms=%d spaces=%d)",
                len(items), len(perception.get("emails", [])),
                len(perception.get("dms", [])), len(perception.get("spaces", [])))

    notify = perception.get("_notify_items", [])
    if not propose:
        # perceive + log only; don't run agents or propose
        return {"count": len(items), "proposed": 0, "items": [
            {"kind": it["kind"], "summary": _item_summary(it)} for it in items
        ], "notify": notify, "notify_count": len(notify), "mode": "perceive_only"}

    proposed, out_items = 0, []
    for it in items:
        payload = None
        try:
            if it["kind"] == "email":
                payload = await _draft_email_reply(it["data"], perception)
            elif it["kind"] in ("gchat", "gchat_space"):
                payload = await _draft_gchat_reply(it["data"], perception)
        except Exception as e:
            logger.warning("inbox: drafting failed for %s: %s", it["kind"], e)
            continue

        if not payload:
            continue

        try:
            broker_result = await broker.propose(**payload)
            proposed += 1
            out_items.append({
                "kind": it["kind"],
                "description": payload["description"],
                "broker_result": broker_result,
            })
        except Exception as e:
            logger.warning("inbox: broker.propose failed for %s: %s", it["kind"], e)

    return {"count": len(items), "proposed": proposed, "items": out_items,
            "notify": notify, "notify_count": len(notify)}


def _item_summary(it: dict) -> str:
    data = it.get("data", {})
    if it["kind"] == "email":
        return f"{data.get('from','')} · {data.get('subject','')[:60]}"
    if it["kind"] == "gchat":
        return f"DM with {data.get('person','')}"
    if it["kind"] == "gchat_space":
        return f"Space: {data.get('display_name','')}"
    return it["kind"]
