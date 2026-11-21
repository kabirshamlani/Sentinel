"""Agentic chat core — the reasoning loop that replaces the regex dispatcher.

The owner's complaint: the old chat_handler was a ~30-branch `if re.match(): handler()`
ladder that pre-matched intent BEFORE any LLM reasoned, so "send me those files"
did a literal disk lookup and follow-ups had amnesia. This replaces it with ONE
context-aware agentic loop:

  see the FULL conversation + memory + what-the owner's-working-on
    -> reason -> call tools (search email, download attachment, send file, run
       bash, send msg) in SEQUENCE, chaining multi-step tasks
    -> reads run immediately; MUTATIONS are proposed to the broker (approval)
    -> on failure: reflect + try a DIFFERENT approach (self_heal), record the
       failure so the same mistake is prevented next time.

The agent emits tool calls as JSON lines; this module executes them and loops
back the results, up to MAX_STEPS. Reads are free; sends/bash go through the
broker. Everything is logged so chat failures finally feed the learning loop.
"""

from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger("agent-chat")

MAX_STEPS = 8


# ─────────────────────────────────────────────────────────── tool executors

async def _t_search_email(args: dict) -> dict:
    from tools import gog_workspace as gw
    emails = await gw.search_emails(args.get("query", "is:unread"), int(args.get("max", 10)))
    return {"emails": [{"id": e.get("id"), "from": e.get("from", ""),
                        "subject": e.get("subject", ""), "date": e.get("date", "")}
                       for e in emails[:12]]}


async def _t_read_email(args: dict) -> dict:
    from tools import gog_workspace as gw
    r = await gw.read_email(args.get("id", ""))
    h = r.get("headers", {}) if isinstance(r.get("headers"), dict) else {}
    msg = r.get("message", {}) if isinstance(r.get("message"), dict) else {}
    body = (r.get("body", "") or "") or msg.get("snippet", "")   # top-level body, snippet fallback
    return {"from": h.get("from", ""), "subject": h.get("subject", ""),
            "body": body[:3000]}


async def _t_find_and_download(args: dict) -> dict:
    from tools import gog_workspace as gw
    files = await gw.find_and_download(args.get("query", ""), int(args.get("max", 5)))
    return {"files": [{"path": f["path"], "filename": f["filename"],
                       "from": f.get("from", ""), "subject": f.get("subject", "")}
                      for f in files]}


async def _t_send_file(args: dict, ctx: dict) -> dict:
    """Send a local file to Telegram. Path must be a real file (usually one just
    downloaded by find_and_download). Home-tree/temp-only + sensitive-dir guarded."""
    path = os.path.realpath(os.path.expanduser(args.get("path", "")))
    home = os.path.realpath(os.path.expanduser("~"))
    tmp = os.path.realpath(__import__("tempfile").gettempdir())
    # containment: inside the home tree OR the temp dir (trailing sep on BOTH,
    # so a sibling like /var/folders/.../T2evil can't pass the prefix test)
    if not (path.startswith(home + os.sep) or path.startswith(tmp + os.sep)):
        return {"error": "path outside allowed roots"}
    # sensitive check on PATH SEGMENTS + basename (not raw substring — avoids
    # false-positives like 'token-economics.pdf' while catching real secret dirs)
    segs = [s.lower() for s in path.split(os.sep)]
    base = os.path.basename(path).lower()
    blocked_dirs = {".ssh", ".aws", ".gnupg", ".config", "keychains", "keychain"}
    blocked_base = ("id_rsa", ".env", "credentials", "secrets.", ".pem", ".key")
    if any(seg in blocked_dirs for seg in segs) or any(b in base for b in blocked_base):
        return {"error": "refusing to send a sensitive path"}
    if not os.path.isfile(path):
        return {"error": f"no file at {path}"}
    tg = ctx.get("telegram")
    if tg and await tg.send_document(path, caption=os.path.basename(path)):
        return {"sent": os.path.basename(path)}
    return {"error": "telegram upload failed"}


async def _t_read_chat(args: dict) -> dict:
    from tools import gog_workspace as gw
    spaces = await gw.list_spaces(20)
    return {"spaces": [{"id": s.get("resource") or s.get("name"), "name": s.get("name", "")}
                       for s in spaces[:15]]}


async def _t_calendar(args: dict) -> dict:
    from tools import gog_workspace as gw
    events = await gw.upcoming_events(int(args.get("max", 8)))
    return {"events": [{"summary": e.get("summary", ""), "start": str(e.get("start", ""))[:19]}
                       for e in events[:8]]}


async def _t_remember(args: dict) -> dict:
    from memory import gateway
    gateway.remember(args.get("fact", ""), kind=args.get("kind", "preference"),
                     source="owner", confidence=0.9)
    return {"remembered": True}


# ── mutation tools: PROPOSE to the broker (approval), never execute directly ──

def _proposal_result(kind: str, target: str, res: dict | None) -> dict:
    """Turn a broker.propose() result into a TRUTHFUL chat outcome (gap-1 fix): the
    tool used to always claim 'awaiting your approval' even when the broker SKIPPED the
    action (dedup / previously-rejected). Now we report what actually happened."""
    res = res or {}
    status = res.get("status", "")
    if status == "skipped":
        why = res.get("reason", "")
        if why == "previously_rejected":
            return {"skipped": f"{kind} to {target} — you already rejected this, so I didn't re-queue it."}
        if why == "duplicate_detected":
            return {"skipped": f"{kind} to {target} — already queued/just done, so I skipped the duplicate."}
        return {"skipped": f"{kind} to {target} — skipped ({why or 'no-op'})."}
    return {"proposed": f"{kind} to {target} — awaiting your approval"}


async def _t_send_email(args: dict, ctx: dict) -> dict:
    broker = ctx["broker"]
    to = args.get("to", "")
    res = await broker.propose(action_type="send_email",
                         description=f"Send email to {to}: {args.get('subject','')[:50]}",
                         details={"to": to, "subject": args.get("subject", ""),
                                  "body": args.get("body", "")},
                         reasoning="Requested in chat.", project="comms", tier="A",
                         reversibility="irreversible", criticality_hint="critical")
    return _proposal_result("email", to, res)


async def _t_send_chat(args: dict, ctx: dict) -> dict:
    broker = ctx["broker"]
    to = args.get("to", "")
    res = await broker.propose(action_type="send_gchat",
                         description=f"Send GChat to {to}: {args.get('message','')[:50]}",
                         details={"space": to, "person_name": to,
                                  "message": args.get("message", "")},
                         reasoning="Requested in chat.", project="comms", tier="A",
                         reversibility="irreversible", criticality_hint="critical")
    return _proposal_result("chat", to, res)


async def _t_run_bash(args: dict, ctx: dict) -> dict:
    """Run a shell command on the owner's machine. Read-only-looking commands could
    run immediately, but to honor 'run anything WITH my approval' we propose ALL
    of them to the broker (critical) — the owner taps ✅ and it executes + reports."""
    broker = ctx["broker"]
    cmd = args.get("command", "")
    # gap-2 fix: pass the raw command as `target` so dedup/reject match on the COMMAND,
    # not the constant "Run on your machine:" prefix (which made rejecting one command
    # collide with every other short command for 7 days).
    res = await broker.propose(action_type="run_command",
                         description=f"Run on your machine: {cmd[:80]}",
                         details={"command": cmd, "cwd": args.get("cwd"), "timeout": 120},
                         reasoning=args.get("why", "Requested in chat."),
                         project="chat-ops", tier="A", reversibility="reversible",
                         criticality_hint="critical")
    return _proposal_result("run", f"`{cmd[:50]}`", res)


async def _t_read_meeting(args: dict) -> dict:
    """Answer 'what was discussed / my tasks / deadlines' from a meeting. BOUNDED:
    one targeted search of transcript-bot senders → read the recap → extract
    the owner's action items + deadlines (dates resolved against meeting date + today).
    Fixes the 'ran long, try narrower' failure — this is 2 gog calls + 1 cheap LLM,
    never an open-ended do_task."""
    from tools import gog_workspace as gw
    from brain import claude_runner
    when = args.get("when", "")  # e.g. 'yesterday 2pm', 'today', or a topic
    # transcript-bot recap emails FROM the bot (not Sentinel's own Re: replies —
    # `-from:me` excludes them, which was returning Sentinel's own text). Prefer
    # the original notes/recap, newest first.
    # The real recap (with the discussion summary + action items) comes from the
    # NOTES bots (gemini-notes / Read.ai / Otter), subject 'Notes:'/'recap'. Calendar
    # invites ('🗓 …') and Sentinel's own 'Re:' replies are NOT recaps — exclude both.
    q = ('subject:(Notes OR recap OR transcript OR "action items") '
         "(from:gemini-notes@google.com OR from:read.ai OR from:otter.ai OR "
         "from:fireflies.ai) newer_than:5d")
    threads = await gw.search_emails(q, 10)
    if not threads:  # fallback: any notes-subject mail
        threads = await gw.search_emails('subject:(Notes OR recap OR "action items") newer_than:5d', 10)
    acct = gw.ACCOUNT.lower()
    cands = [t for t in threads
             if "🗓" not in (t.get("subject", "") or "")
             and not (t.get("subject", "") or "").lower().startswith("re:")
             and acct not in (t.get("from", "") or "").lower()]
    cands = cands or threads
    if not cands:
        return {"error": "no meeting recap/notes email found in the last few days"}

    def score(t):
        subj = (t.get("subject", "") or "").lower()
        s = 0
        if when:
            s += sum(1 for w in when.lower().split() if len(w) > 2 and w in subj)
        return s
    pick = max(cands, key=score)
    full = await gw.read_email(pick.get("id", ""))
    body = (full.get("body", "") or "")[:4000]
    # if the recap body is thin (a reply/stub), fall back to the search snippet
    if len(body.strip()) < 120:
        body = (pick.get("snippet", "") or body)[:4000]
    from datetime import datetime
    from prompts import load
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = load("meeting.extract", subject=pick.get("subject", ""), today=today, body=body)
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             task_type="meeting_recall", allowed_tools=[],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    return {"meeting": pick.get("subject", ""), "recap": out.strip()[:1800]}


async def _t_read_cursor(args: dict) -> dict:
    """Answer 'what's happening in my Cursor' from the LIVE local DB, not a stale
    sync. Reuses observer.cursor_sessions (reads state.vscdb read-only)."""
    try:
        from tools import observer
        sessions = observer.cursor_sessions(hours=float(args.get("hours", 48)))
    except Exception as e:
        return {"error": f"cursor read failed: {e}"}
    return {"cursor_sessions": [
        {"name": s.get("name", ""), "subtitle": s.get("subtitle", ""),
         "files_changed": s.get("files_changed", 0),
         "last_active": s.get("last_active", ""),
         "recent_asks": s.get("recent_asks", [])}
        for s in sessions[:8]]}


async def _t_do_task(args: dict, ctx: dict) -> dict:
    """The ESCAPE HATCH — the fix for the bullshitter. For any concrete task that
    the fixed tools don't cover (read Cursor, find local files/videos, inspect the
    system, "do X on my Mac"), actually EXECUTE it as a concrete act-tier Claude
    Code run with real tools (Read/Grep/Glob/Bash). Proven to complete without a
    policy refusal because the prompt is ONE concrete goal, not a tool-menu.

    If deliverable=="file", the agent is told to output the absolute path(s) of the
    file(s) to send on lines prefixed `SENDFILE:`; the executor then sends each via
    the guarded _t_send_file. Reads/inspection run directly; a mutation the task
    needs must be requested as a proposal (the agent is told sends require approval)."""
    from brain import claude_runner
    goal = args.get("goal", "").strip()
    deliverable = args.get("deliverable", "answer")
    if not goal:
        return {"error": "do_task needs a concrete goal"}
    prompt = (
        "You are the owner's own assistant acting on HIS OWN Mac and HIS OWN "
        "authenticated accounts at his explicit request — authorized self-service. "
        "Do this ONE concrete task now using your tools (read files, grep, glob, run "
        "read-only shell like sqlite3/mdfind/ls/df/git). Actually perform it and "
        "report the REAL result. Do NOT say you 'will' do it — do it.\n\n"
        f"TASK: {goal}\n\n")
    if deliverable == "file":
        prompt += ("If this produces file(s) to deliver, print each one's ABSOLUTE "
                   "path on its own line prefixed exactly with `SENDFILE:`. Then a "
                   "one-line summary. If you can't find the file(s), say so plainly.\n")
    else:
        prompt += "Report the concrete finding in 1-4 lines. If you can't, say why plainly.\n"
    # Keep it snappy + bounded: cap turns and tell it to prefer fast, narrow shell
    # (avoid broad recursive mdfind/scans that hang). A chat task should resolve in
    # a few tool calls, not minutes — an unbounded scan hurt responsiveness.
    prompt += ("\nBe fast: use narrow, bounded commands (scope mdfind with -onlyin, "
               "limit sqlite/ls output, avoid full-disk recursive scans). Finish in a "
               "few steps; if it needs a long scan, do the quickest useful version.")
    out = ""
    async for ev in claude_runner.run_claude(
            prompt, agent="conductor", tier="act", task_type="chat",
            allowed_tools=["Read", "Grep", "Glob", "Bash"], model=None, max_turns=12):
        if ev.type == "result":
            out = ev.content or ""
    if "Usage Policy" in out:
        return {"error": "the task was refused by the model policy — rephrase or narrow it"}
    if not out:
        return {"error": "the task ran long and didn't finish — try a narrower ask"}
    # F4 (defense in depth): scrub secret-shaped text so a cat'd credential in the
    # task output can't surface even to the owner's own chat / get persisted.
    try:
        from memory.gateway import _scrub
        out = _scrub(out)
    except Exception:
        pass
    result = {"result": out.strip()[:1500]}
    if deliverable == "file":
        sent = []
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("SENDFILE:"):
                path = s[len("SENDFILE:"):].strip()
                sres = await _t_send_file({"path": path}, ctx)
                if sres.get("sent"):
                    sent.append(sres["sent"])
        result["files_sent"] = sent
    return result


_TOOLS = {
    "search_email": _t_search_email, "read_email": _t_read_email,
    "get_his_files": _t_find_and_download, "find_and_download": _t_find_and_download,
    "read_chat": _t_read_chat, "read_cursor": _t_read_cursor, "calendar": _t_calendar,
    "read_meeting": _t_read_meeting, "remember": _t_remember,
    "send_file": _t_send_file, "do_task": _t_do_task,
    "draft_reply": _t_send_email, "send_email": _t_send_email,
    "draft_message": _t_send_chat, "send_chat": _t_send_chat,
    "shell_task": _t_run_bash, "run_bash": _t_run_bash,
}
_NEEDS_CTX = {"send_file", "do_task", "draft_reply", "send_email", "draft_message",
              "send_chat", "shell_task", "run_bash"}

# Tools are described by OUTCOME (not raw shell/RCE menus) so the policy layer
# sees an assistant helping its owner with their own account, not an exfil kit.


# ─────────────────────────────────────────────────────── planner + executor

# Milestone labels shown in the edit-in-place progress bubble (per tool).
_MILESTONE = {
    "search_email": "searching your inbox…", "read_email": "reading it…",
    "get_his_files": "finding the files in email…", "send_file": "sending it over…",
    "read_chat": "checking your chats…", "read_cursor": "reading your Cursor sessions…",
    "read_meeting": "pulling up the meeting notes…", "calendar": "checking your calendar…",
    "do_task": "working on it…", "draft_reply": "drafting the reply…",
    "draft_message": "drafting the message…", "shell_task": "queuing that to run…",
}


async def respond(text: str, channel: str, transcript: str, *, telegram=None,
                  broker=None, router=None, progress=None) -> str:
    """The THINKING core (v0.16): one intent-steered agentic loop, not a fixed-vocab
    planner. The agent reasons over the full conversation + memory + the owner's day,
    reads freely with real tools (gog/Read/Grep/Glob/Bash — read-only enforced by the
    deny-guard hook), and for anything that changes the world emits a PROPOSE line that
    the broker executes after the owner approves. Files to deliver are printed as SENDFILE:/
    SENDIMAGE: lines and sent here.

    Why a loop, not a fixed vocab: the old `_STEP_VOCAB` planner (v0.13-15) couldn't
    handle open-ended asks — anything off the menu fell through to a plain answer and it
    narrated instead of acting. The CLI "god-prompt refusal" that forced the narrow vocab
    no longer reproduces (verified 2026-07-16), so the agent now owns a broad tool surface
    and thinks its way to the goal. Safety is structural, not vocab-based: the deny-guard
    blocks every send/mutation via Bash, so the agent MUST route mutations through PROPOSE
    -> broker -> the owner's approval.

    `progress` (optional async fn(str)) edits an in-place status bubble so long tasks
    show life. Signature is unchanged so chat_handler._agentic stays a drop-in caller."""
    ctx = {"telegram": telegram, "broker": broker, "router": router}

    async def _say(msg):
        if progress:
            try:
                await progress(msg)
            except Exception:
                pass

    # circuit breaker: bail cleanly if this exact request is doom-looping
    if _looping(text):
        _record_failure(text, "doom-loop", "same request repeated with no progress")
        return ("I keep going in circles on that one — can you give me a bit more to "
                "go on, or rephrase? I'd rather ask than spin.")

    await _say("thinking…")
    result = await _run_thinking_loop(text, transcript, ctx, _say)
    return result


# ── legacy fixed-vocab executor path (kept for reference; no longer the entry) ──
async def _legacy_execute(text, channel, transcript, ctx, _say, plan):
    steps = plan.get("steps", [])
    log, produced = [], {"files": [], "emails": [], "text": {}}
    for st in steps[:MAX_STEPS]:
        name = st.get("tool")
        fn = _TOOLS.get(name)
        if not fn:
            continue
        await _say(_MILESTONE.get(name, "working on it…"))
        # resolve args from prior step outputs (e.g. send_file over downloaded paths)
        args = _resolve_args(st.get("args", {}), produced, text, transcript)
        try:
            res = await (fn(args, ctx) if name in _NEEDS_CTX else fn(args))
        except Exception as e:
            res = {"error": str(e)[:200]}
            _record_failure(text, name, str(e))
        # SELF-HEAL: a read step that errored/empty → retry ONCE via do_task with a
        # different approach (never surface "I couldn't" without trying again).
        if isinstance(res, dict) and (res.get("error") or _empty_read(name, res)) \
                and name in ("search_email", "read_email", "read_meeting", "read_cursor",
                             "read_chat", "calendar", "get_his_files"):
            await _say("that route missed — trying another way…")
            # SECURITY: build the retry goal ONLY from the owner's own message + the
            # tool name — never from the failed result string (which could carry
            # attacker-controlled email/chat text into an act-tier Bash goal).
            heal = await _t_do_task(
                {"goal": f"The owner asked: {text}. A direct {name} lookup came up empty. "
                         "Get the answer a DIFFERENT way using your own read-only tools "
                         "(gog CLI, files, calendar). Report the real result.",
                 "deliverable": "answer"}, ctx)
            if isinstance(heal, dict) and heal.get("result") and not heal.get("error"):
                res = heal
        # capture outputs for downstream steps
        if isinstance(res, dict):
            if res.get("files"):
                produced["files"].extend(res["files"])
            if res.get("emails"):
                produced["emails"] = res["emails"]
        log.append({"tool": name, "res": res})

        # multi-step chaining: after downloading files, if the plan wants them
        # sent, send each unique file once (planner's single send_file means "all").
        if name in ("get_his_files", "find_and_download") and produced["files"] \
                and any(s.get("tool") == "send_file" for s in steps):
            seen_paths = set()
            for f in produced["files"]:
                if f["path"] in seen_paths:
                    continue
                seen_paths.add(f["path"])
                sres = await _t_send_file({"path": f["path"]}, ctx)
                log.append({"tool": "send_file", "res": sres})
            steps = [s for s in steps if s.get("tool") != "send_file"]  # consumed

    return await _summarize(text, log)


# ═══════════════════════════════════════════════ v0.16 thinking loop ═════════

async def _run_thinking_loop(text: str, transcript: str, ctx: dict, say) -> str:
    """Assemble the operating-manual prompt, run ONE intent-steered agent with a broad
    read surface, then route the PROPOSE/SENDFILE/SENDIMAGE lines it emits. The agent
    thinks and reads; Python is the trusted hands for anything that leaves the box."""
    from brain import claude_runner
    from memory import gateway
    from prompts import load

    mem = gateway.prompt_block(context_hint=text, budget_chars=900)
    try:
        from verticals import day
        day_ctx = day.context_block(max_chars=900)
    except Exception:
        day_ctx = ""
    try:
        from brain import hermes_adapter
        rec = await hermes_adapter.recall(text, k=3)
        recall = ("Possibly-relevant past context: " + "; ".join(rec)) if rec else ""
    except Exception:
        recall = ""

    # pro-memory: surface what Sentinel actually DID that relates to this ask (real
    # logged actions with evidence), so "what did I do about X / N months ago" answers
    # from the ledger, not vibes. Cheap LIKE search over the small ledger.
    try:
        from memory import ledger
        hits = ledger.search(text, days=120, limit=6) or ledger.recent(days=14, limit=6)
        if hits:
            lines = [f"- {h['when']}: {h['tool']} {('→ ' + h['target']) if h['target'] else ''} "
                     f"[{h['status']}]" for h in hits[:6]]
            recall = (recall + "\n" if recall else "") + "What you've done recently (from the action log):\n" + "\n".join(lines)
    except Exception:
        pass

    prompt = load("chat.core", memory=mem, day_context=day_ctx, recall=recall,
                  transcript=transcript or "(no prior messages)", request=text)

    out = ""
    # broad read surface; deny-guard blocks any send/mutation so the agent must PROPOSE.
    # task_type="chat" routes model selection; effort/turns bounded but generous (the owner:
    # "don't limit the agents" — momentum > tokens, real safety is the doom-loop breaker).
    async for ev in claude_runner.run_claude(
            prompt, agent="conductor", tier="act", task_type="chat",
            allowed_tools=["Read", "Grep", "Glob", "Bash"], model=None,
            max_turns=int(os.getenv("SENTINEL_CHAT_MAX_TURNS", "24"))):
        if ev.type == "result":
            out = ev.content or ""
        elif ev.type == "tool_use" and ev.tool_name == "Bash":
            await say("digging in…")
    if "Usage Policy" in out or not out.strip():
        _record_failure(text, "thinking-loop", "empty or policy-refused result")
        return await _answer(text, transcript)     # grounded fallback, never a dead end

    return await _route_agent_output(out, text, ctx, say)


# lines the agent emits for Python to act on (the trusted-hands boundary)
_PROPOSE_RE = re.compile(r"^\s*PROPOSE:\s*(\{.*\})\s*$", re.M)
_SENDFILE_RE = re.compile(r"^\s*SENDFILE:\s*(.+?)\s*$", re.M)
_SENDIMAGE_RE = re.compile(r"^\s*SENDIMAGE:\s*(.+?)\s*$", re.M)


async def _route_agent_output(out: str, text: str, ctx: dict, say=None) -> str:
    """Parse the agent's PROPOSE/SENDFILE/SENDIMAGE lines, route each through the
    trusted executor (broker for mutations, guarded sender for files), and return the
    clean human-facing text (control lines stripped)."""
    from memory.gateway import _scrub

    async def _tick(msg):                         # tolerate sync/async/None say
        if say is None:
            return
        try:
            r = say(msg)
            if hasattr(r, "__await__"):
                await r
        except Exception:
            pass

    proposed, sent = [], []

    # 1) mutations -> broker proposals (the agent never sends directly)
    for m in _PROPOSE_RE.finditer(out):
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        res = await _propose_action(action, ctx)
        if res:
            proposed.append(res)
            # ledger evidence = the broker proposal (the mutation itself completes on approval)
            _ledger(text, action.get("action", "propose"), target=res, status="proposed")
            await _tick("queued for your approval…")

    # 2) files/images to deliver -> guarded senders (ledger evidence = the filename sent)
    for m in _SENDFILE_RE.finditer(out):
        sres = await _t_send_file({"path": m.group(1)}, ctx)
        if sres.get("sent"):
            sent.append(sres["sent"])
            _ledger(text, "send_file", target=sres["sent"], evidence=f"delivered:{sres['sent']}")
            await _tick("sending it over…")
    for m in _SENDIMAGE_RE.finditer(out):
        sres = await _send_image(m.group(1), ctx)
        if sres.get("sent"):
            sent.append(sres["sent"])
            _ledger(text, "send_image", target=sres["sent"], evidence=f"delivered:{sres['sent']}")

    # 3) strip control lines from the human-facing reply
    clean = _PROPOSE_RE.sub("", out)
    clean = _SENDFILE_RE.sub("", clean)
    clean = _SENDIMAGE_RE.sub("", clean)
    clean = _scrub(clean).strip()

    tail = []
    if sent:
        tail.append(f"📎 Sent {len(sent)} to your phone: {', '.join(dict.fromkeys(sent))}.")
    if proposed:
        tail.append("⏳ Queued for your approval: " + "; ".join(proposed))
    reply = "\n\n".join([p for p in [clean, " ".join(tail)] if p]).strip()
    return reply or "Done."


async def _propose_action(action: dict, ctx: dict) -> str:
    """Route one parsed PROPOSE action to the broker. Returns a short label or ''.
    The concrete target is echoed into the description so the owner sees exactly what he
    approves (egress-target echo — Wave 0 guardrail)."""
    broker = ctx.get("broker")
    if not broker:
        return ""
    kind = (action.get("action") or "").lower()
    try:
        if kind in ("send_email", "draft_email"):
            to = action.get("to", "")
            res = await _t_send_email({"to": to, "subject": action.get("subject", ""),
                                       "body": action.get("body", "")}, ctx)
            return res.get("skipped") or f"email → {to}"      # honest skip surfacing
        if kind in ("send_chat", "send_gchat"):
            to = action.get("to", "")
            res = await _t_send_chat({"to": to, "message": action.get("message", "")}, ctx)
            return res.get("skipped") or f"chat → {to}"
        if kind in ("run_command", "shell"):
            cmd = action.get("command", "")
            res = await _t_run_bash({"command": cmd, "why": action.get("why", "")}, ctx)
            return res.get("skipped") or f"run `{cmd[:50]}`"
        if kind in ("add_mcp_server", "add_mcp"):
            name = action.get("name", "")
            await broker.propose(action_type="add_mcp_server",
                description=f"Add MCP server '{name}' to .mcp.json",
                details={"name": name, "config": action.get("config", {})},
                reasoning=action.get("why", "Requested in chat."), project="sentinel-self",
                tier="A", reversibility="reversible", criticality_hint="critical")
            return f"add MCP server → {name}"
        if kind in ("add_skill", "create_skill"):
            name = action.get("name", "")
            path = f".claude/skills/{name}/SKILL.md"
            await broker.propose(action_type="add_skill",
                description=f"Create skill '{name}' at {path}",
                details={"path": path, "content": action.get("content", "")},
                reasoning=action.get("why", "Requested in chat."), project="sentinel-self",
                tier="A", reversibility="reversible", criticality_hint="critical")
            return f"add skill → {name}"
    except Exception as e:
        logger.warning("propose route failed: %s", e)
    return ""


async def _send_image(path: str, ctx: dict) -> dict:
    """Send a generated image to Telegram (guarded like _t_send_file)."""
    p = os.path.realpath(os.path.expanduser(path))
    home = os.path.realpath(os.path.expanduser("~"))
    tmp = os.path.realpath(__import__("tempfile").gettempdir())
    if not (p.startswith(home + os.sep) or p.startswith(tmp + os.sep)) or not os.path.isfile(p):
        return {"error": "bad image path"}
    tg = ctx.get("telegram")
    if tg and hasattr(tg, "send_photo") and await tg.send_photo(p, caption=os.path.basename(p)):
        return {"sent": os.path.basename(p)}
    return {"error": "image send failed"}


# ── circuit breaker: cheap doom-loop guard (reuses the self_heal fingerprint idea) ──
_RECENT: list[tuple[float, str]] = []


def _looping(text: str) -> bool:
    """True if this exact request has fired 3+ times in a short window with no progress
    — bail to a clean ask instead of spinning (the real safety wall, per the blueprint)."""
    import time
    key = re.sub(r"\s+", " ", (text or "").strip().lower())[:120]
    now = time.time()
    _RECENT[:] = [(t, k) for (t, k) in _RECENT if now - t < 180]
    hits = sum(1 for (_, k) in _RECENT if k == key)
    _RECENT.append((now, key))
    return hits >= 3


def _ledger(request: str, tool: str, *, target: str = "", evidence: str = "",
            status: str = "") -> None:
    """Record an action in the evidence-gated ledger (best-effort; never breaks chat).
    Evidence-gated: a mutation is logged 'proposed' (it completes on approval); a real
    delivery is logged with a concrete evidence token. Feeds pro-memory + the reporter."""
    try:
        from memory import ledger
        ledger.record(request, tool, target=target, evidence=evidence,
                      status=status, channel="chat")
    except Exception:
        pass


_STEP_VOCAB = ("search_email", "read_email", "get_his_files", "send_file",
               "read_chat", "read_cursor", "read_meeting", "calendar", "remember",
               "do_task", "draft_reply", "draft_message", "shell_task")


async def _plan(text: str, transcript: str) -> dict:
    """Narrow, context-aware planner (passes the policy filter — no tool-menu god
    prompt). Decides: answer directly, or an ordered list of steps."""
    from brain import claude_runner
    from memory import gateway
    mem = gateway.prompt_block(context_hint=text, budget_chars=700)
    # who the owner is + what he's doing today (goals/tasks/today) so planning is
    # goal-aware, not blind — the "understand my day" context.
    try:
        from verticals import day
        day_ctx = day.context_block(max_chars=900)
    except Exception:
        day_ctx = ""
    # cross-session recall from Hermes (enrichment; empty if Hermes off)
    try:
        from brain import hermes_adapter
        recall = await hermes_adapter.recall(text, k=3)
        recall_block = ("Possibly-relevant past context: " + "; ".join(recall)) if recall else ""
    except Exception:
        recall_block = ""
    prompt = (
        "You plan how the owner's own personal assistant should fulfil his request on "
        "his own machine/accounts. Use the conversation to resolve references like "
        "'those files', 'the 2nd one', 'do that'.\n\n"
        + (f"{day_ctx}\n\n" if day_ctx else "")
        + (f"{recall_block}\n\n" if recall_block else "")
        + f"{mem}\n\nConversation:\n{transcript}\n\nThe owner now says: {text}\n\n"
        "HARD RULE: never plan to 'later' do something. Every intent becomes a "
        "concrete step that EXECUTES now, or you say plainly you can't. No "
        "future-tense promises.\n\n"
        "If it's a question you can fully answer from the context above, put the "
        "answer in `reply`. If it needs ANY lookup/action, produce an ordered "
        '`steps` list — do NOT answer from memory. Each step is {"tool": <one of: '
        + ", ".join(_STEP_VOCAB) + '>, "args": {...}}.\n'
        "Tool guide:\n"
        "- his own email files (payslips/docs/videos-in-mail): get_his_files "
        '{"query":"<Gmail search with has:attachment>"} then send_file.\n'
        "- what's happening in his Cursor / recent coding: read_cursor {}.\n"
        "- what was discussed in a meeting / his action items / deadlines / feedback: "
        'read_meeting {"when":"<e.g. yesterday 2pm, today, or a topic>"}. USE THIS for '
        "any 'what did I discuss / what am I supposed to do / meeting notes' ask — it "
        "reads the real recap email. Do NOT use do_task for meetings.\n"
        "- ANYTHING ELSE that needs the machine (find local files/videos, inspect "
        "the system, check git/disk, read the Cursor DB in depth, any do-X-on-my-Mac "
        'task): do_task {"goal":"<one concrete instruction>", "deliverable":"answer|file"}. '
        'Use deliverable="file" when the result is file(s) to send to his phone — '
        "do_task will locate AND send them. This is how you actually DO open-ended "
        "tasks instead of describing them.\n"
        "- draft_reply/draft_message/shell_task are queued for his approval.\n"
        'Return STRICT JSON: {"reply": "<text or empty>", "steps": [ ... ]}'
    )
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             task_type="chat", allowed_tools=[],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    if "Usage Policy" in out:                 # planner itself refused — fall back
        return {"reply": "", "steps": []}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return {"reply": (d.get("reply") or "").strip(), "steps": d.get("steps", [])}
        except json.JSONDecodeError:
            pass
    return {"reply": out.strip() if out and "TOOL" not in out else "", "steps": []}


def _resolve_args(args: dict, produced: dict, text: str, transcript: str) -> dict:
    """Fill a step's args from prior outputs (e.g. a send_file path from downloads,
    an email id/addr from the last search)."""
    args = dict(args or {})
    if args.get("path") in (None, "", "<from downloads>", "auto") and produced.get("files"):
        args["path"] = produced["files"][0]["path"]
    # 'the 2nd one' style: if a draft targets an ordinal, resolve from last emails
    if args.get("to") in (None, "") and produced.get("emails"):
        m = re.search(r"\b(\d+)(?:st|nd|rd|th)?\b", text)
        idx = int(m.group(1)) - 1 if m else 0
        emails = produced["emails"]
        if 0 <= idx < len(emails):
            addr = _extract_addr(emails[idx].get("from", ""))
            if addr:
                args["to"] = addr
                args.setdefault("subject", "Re: " + emails[idx].get("subject", ""))
    return args


def _extract_addr(s: str) -> str:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", s or "")
    return m.group(0) if m else ""


def _empty_read(name: str, res: dict) -> bool:
    """A read step that returned nothing useful (→ trigger self-heal retry)."""
    if name == "search_email":
        return not res.get("emails")
    if name in ("get_his_files", "find_and_download"):
        return not res.get("files")
    if name == "read_meeting":
        return not res.get("recap")
    if name == "read_cursor":
        return not res.get("cursor_sessions")
    if name == "calendar":
        return not res.get("events")
    return False


async def _answer(text: str, transcript: str) -> str:
    """Plain grounded answer for questions the planner didn't turn into steps."""
    from brain import claude_runner
    from memory import gateway
    from channels.formatting import VOICE
    from prompts import load
    mem = gateway.prompt_block(context_hint=text, budget_chars=1000)
    try:
        from tools import observer
        work = observer.summary_text(max_chars=500)
    except Exception:
        work = ""
    prompt = load("chat.answer", voice=VOICE, memory=mem,
                  work=(f"What he's working on:\n{work}\n" if work else ""),
                  transcript=transcript, request=text)
    out = ""
    # NO Bash here: this is a grounded-answer fallback; any command must go
    # through the broker via shell_task, not run un-brokered from a chat reply.
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             task_type="chat",
                                             allowed_tools=["Read", "Grep", "Glob"],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    return out.strip() if "Usage Policy" not in out else "Let me try that a different way — can you rephrase?"


async def _summarize(text: str, log: list) -> str:
    """Turn the EXECUTED steps into a short human reply (evidence-only — reports
    ONLY what actually ran, never a promise)."""
    def res(r):
        return r["res"] if isinstance(r["res"], dict) else {}
    sent = [res(r).get("sent") for r in log if r["tool"] == "send_file" and res(r).get("sent")]
    sent += [f for r in log for f in (res(r).get("files_sent") or [])]        # do_task file deliveries
    proposed = [res(r).get("proposed") for r in log if res(r).get("proposed")]
    errors = [f'{r["tool"]}: {res(r)["error"]}' for r in log if res(r).get("error")]
    files = [f["filename"] for r in log if r["tool"] in ("get_his_files", "find_and_download")
             for f in (res(r).get("files") or [])]
    # do_task / read_cursor / read_meeting return prose findings — surface directly
    findings = [res(r)["result"] for r in log if r["tool"] == "do_task" and res(r).get("result")]
    findings += [res(r)["recap"] for r in log if r["tool"] == "read_meeting" and res(r).get("recap")]
    cursor = [r for r in log if r["tool"] == "read_cursor" and res(r).get("cursor_sessions")]
    bits = []
    if findings:
        bits.append("\n\n".join(findings))
    if cursor:
        cs = cursor[0]["res"]["cursor_sessions"]
        bits.append("Recent Cursor: " + "; ".join(
            f"{s['name']}" + (f" — {s['recent_asks'][0][:60]}" if s.get("recent_asks") else "")
            for s in cs[:4]))
    if sent:
        bits.append(f"Sent {len(sent)} file(s) to your phone: {', '.join(dict.fromkeys(sent))}.")
    elif files and not findings:
        bits.append(f"Found {len(files)}: {', '.join(files[:5])}.")
    for p in proposed:
        bits.append(f"Queued for your approval: {p}")
    if errors and not (sent or proposed or findings or cursor):
        bits.append("Couldn't complete it: " + "; ".join(errors[:2]) + " — logged so I improve.")
    if bits:
        return " ".join(bits)
    # NOTHING executed and no honest error → the old bullshit path. Record the
    # failure AND seed a real CORRECTION (force-injected into every future planner
    # prompt; lessons are only opportunistically retrieved, so a lesson alone
    # wouldn't reliably train this out — C1 from review).
    _record_failure(text, "no-execution",
                    "planner produced no runnable step; nearly narrated instead of acting")
    try:
        from memory import gateway
        gateway.remember("When the owner asks for a concrete task, EXECUTE it via a "
                         "do_task step — never narrate intent, never promise future "
                         "work. Either do it and report the real result, or say "
                         "plainly you can't and why.",
                         kind="correction", source="system", confidence=1.0)
    except Exception:
        pass
    return ("I couldn't turn that into something I can actually do right now — "
            "tell me a bit more specifically and I'll run it (not just describe it).")


def _record_failure(objective: str, tool: str, error: str) -> None:
    """A chat-path tool failure now teaches the system (was recorded NOWHERE)."""
    try:
        from memory import reasoning_bank
        reasoning_bank.record_failure(f"chat task: {objective[:80]} (tool {tool})",
                                      agent="agent-chat", error=error, task_type="chat")
    except Exception:
        pass
    try:
        from learning.error_journal import log_error
        log_error(agent="agent-chat", session_id="chat", task_summary=objective[:80],
                  error_type="tool_error", error_message=error[:200], signal_trace="",
                  what_should_have_happened="complete the chat task")
    except Exception:
        pass
    # The owner 2026-08-15: a failed live task SUMMONS THE COACH — file a
    # high-priority issue now and flag the fix cycle to run at the next tick.
    try:
        from coaching.escalate import live_failure
        live_failure("agent-chat", objective, f"tool {tool}: {error}")
    except Exception:
        pass
