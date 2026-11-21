"""Conversational chat handler — talk to Sentinel like a person, with memory.

What changed vs v0.7 (the owner's feedback):
  * CONVERSATION MEMORY — every message in/out is persisted; the recent
    transcript + structured context slots ("the emails I just showed you") are
    injected into reasoning, so continuation messages and follow-ups
    ("reply to the second one", "do that") just work. /newchat resets.
  * COMMANDS — /newchat /status /tasks /brief /report /memory /models
    /insights /mute /unmute /help (registered in Telegram's menu).
  * VOICE — replies are written like a human colleague (formatting.VOICE) and
    rendered per channel (Telegram HTML etc.) so no raw ** ever shows.

Safety unchanged: reads are immediate; anything that changes state (send mail
as the owner, open PR, mutate infra) becomes a broker proposal he approves.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from channels.conversation import Conversation, ordinal_in
from channels.formatting import VOICE

logger = logging.getLogger("chat")

_ALLOWED_READ_ROOTS = [str(Path.home())]
_BLOCKED_PATH_MARKERS = (".ssh", ".aws", "keychain", "/.gnupg", "credential",
                         "id_rsa", ".env", "token", "secret")

# Natural approval phrasing ("approved all and fix", "yes do it all") contains
# extra words and never equals a fixed token. A bare CONTAINS match is unsafe
# — "ok so how does the coach work" would silently approve a pending critical
# action. Rule: the WHOLE short message must be approval vocabulary and carry
# at least one strong approval word; anything conversational falls through to
# chat untouched.
_APPROVE_WORDS = {"approve", "approved", "yes", "ok", "okay", "go", "do", "it",
                  "ship", "all", "everything", "them", "both", "and", "fix",
                  "fixes", "coach", "coaches", "coaching", "merge", "merges",
                  "pr", "prs", "the", "please", "sure", "now"}
_APPROVE_STRONG = {"approve", "approved", "yes", "ok", "okay", "go", "ship"}
_REJECT_WORDS = {"reject", "rejected", "no", "cancel", "stop", "all",
                 "everything", "them", "both", "and", "please"}
_REJECT_STRONG = {"reject", "rejected", "no", "cancel", "stop"}


def _resolution_intent(low: str, vocab: set, strong: set) -> bool:
    """True when a short message is made ENTIRELY of resolution vocabulary
    with at least one strong verb — never a substring match inside chat."""
    words = re.findall(r"[a-z']+", low)
    return 0 < len(words) <= 8 and set(words) <= vocab and bool(set(words) & strong)

HELP = (
    "I'm Sentinel — just talk to me like a person. I remember our conversation.\n\n"
    "Things I do:\n"
    "• status / tasks — what I've done, what's pending\n"
    "• brief — what you missed (email, chat, calendar)\n"
    "• check my email · check chat · reply to <email>: <msg>\n"
    "• send me <path> — file from this Mac\n"
    "• research <question> — deep web research\n"
    "• fix issue 3 in owner/repo — work it → PR approval\n"
    "• remember <fact> — I'll never forget it\n"
    "• /newchat — fresh conversation · /mute 2h — pause pings\n"
    "• /memory /models /insights — what I know, model stats, work insights\n"
    "…or ask anything. I can read this machine, your mail, chat and calendar."
)


class ChatHandler:
    def __init__(self, router, config: dict | None = None):
        self.router = router
        self.config = config or {}
        self._telegram = None
        self._lock = asyncio.Lock()   # one message at a time: no cross-channel wire-crossing

    def bind_telegram(self, tg):
        self._telegram = tg

    # ------------------------------------------------------------------ entry

    async def handle(self, text: str, channel: str = "telegram", reply_via=None) -> None:
        """Route one inbound message. reply_via: optional async fn(text) used by
        non-Telegram bridges (e.g. GChat) to answer in-place."""
        async with self._lock:
            await self._handle(text, channel, reply_via)

    async def _handle(self, text: str, channel: str, reply_via) -> None:
        t = text.strip()
        low = t.lower().rstrip("?!. ")
        convo = Conversation(channel)
        convo.add("user", t)
        self._reply_via = reply_via
        self._convo = convo
        try:
            # -- commands (slash or bare) --
            cmd = low.lstrip("/").split()[0] if low else ""
            if low in ("/start",) or cmd in ("help",):
                return await self._reply(HELP)
            if cmd in ("newchat", "new", "reset"):
                convo.clear()
                return await self._reply("🌱 Fresh chat — context cleared. What's next?")
            if cmd == "mute":
                hours = _parse_hours(t) or 3.0
                from conductor import kv
                kv.mute_for(hours)
                return await self._reply(f"🔇 Muted proactive pings for {hours:g}h. "
                                         f"Approvals still come through. /unmute to resume.")
            if cmd in ("unmute", "wake"):
                from conductor import kv
                kv.unmute()
                return await self._reply("🔔 Back on — proactive updates resumed.")
            if cmd == "afk" or low in ("going afk", "i'm afk", "im afk", "afk now"):
                hours = _parse_hours(t) or 4.0
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                from conductor import kv
                kv.put("force_idle", {"until": (_dt.now(_tz.utc) + _td(hours=hours)).isoformat(),
                                      "set_at": _dt.now(_tz.utc).isoformat()})
                return await self._reply(
                    f"🏃 AFK mode for {hours:g}h — the machine counts as free NOW "
                    f"(idle + battery gates bypassed), so fix cycles and heavy work "
                    f"run immediately. `/back` cancels.")
            if cmd in ("back", "unafk") or low in ("i'm back", "im back"):
                from conductor import kv
                kv.put("force_idle", None)
                return await self._reply("👋 Welcome back — heavy work returns to "
                                         "idle-gated (runs only when you're away).")
            if cmd in ("status", "report") or low in ("what's up", "whats up"):
                return await self._status(full=(cmd == "report"))
            if cmd == "tasks":
                return await self._tasks()
            if cmd in ("brief", "briefing") or low in ("what did i miss", "whats new", "what's new", "brief me"):
                return await self._whats_new()
            if cmd == "memory":
                return await self._memory_summary()
            if cmd == "models":
                return await self._models()
            if cmd == "insights":
                return await self._insights()
            if cmd == "autonomy":
                from approval import autonomy
                return await self._reply(autonomy.status_text())
            if cmd == "health":
                from conductor import observer
                return await self._reply(observer.health_text())
            if cmd == "day" or low in ("what's my day", "whats my day", "my day"):
                from verticals import day
                td = day.today_text()
                if not td:
                    await self._reply("Building your day…")
                    td = await day.build_today()
                return await self._reply(td or "Nothing scheduled that I can see.")
            m = re.match(r"^goals?\s*[:\-]\s*(.+)$", t, re.I | re.S)
            if m:
                from verticals import day
                day.set_goals(m.group(1).strip())
                return await self._reply("Got it — updated your goals. I'll filter what "
                                         "matters through these from now on. 🎯")
            if cmd in ("cost", "spend", "budget"):
                from conductor import kv
                led = kv.get("cost_ledger", {}) or {}
                if not led.get("runs"):
                    return await self._reply("💸 No model spend recorded yet today.")
                top = sorted(led.get("by_agent", {}).items(), key=lambda x: -x[1])[:5]
                lines = [f"💸 **Today's model spend**: ${led.get('cost_usd',0):.3f} "
                         f"over {led.get('runs',0)} runs "
                         f"({led.get('in',0)//1000}k in / {led.get('out',0)//1000}k out tokens)"]
                for a, c in top:
                    lines.append(f"• {a}: ${c:.3f}")
                return await self._reply("\n".join(lines))
            if cmd in ("cleardrafts", "clear_drafts", "clean_drafts"):
                from tools import gog_workspace as gw
                res = await gw.clear_junk_drafts(dry_run=False)
                n, kept = res.get("removed", 0), res.get("kept", 0)
                if n:
                    return await self._reply(f"🧹 Deleted {n} empty junk draft(s) from your "
                                             f"Gmail. Kept {kept} real one(s).")
                return await self._reply(f"No junk drafts to clear — {kept} real draft(s) left alone.")
            if cmd == "hermes":
                from brain import hermes_adapter
                return await self._reply(hermes_adapter.status_text())
            if cmd == "army":
                from conductor import agent_registry
                return await self._reply(agent_registry.summary())
            if cmd in ("upgrades", "update"):
                from learning import self_update
                ups = await self_update.check_updates()
                if not ups:
                    return await self._reply("🔄 Everything's up to date. " + self_update.status_text())
                lines = ["🔄 **Upgrades available** (I'll propose each for your ✅):"]
                for u in ups:
                    lines.append(f"• {u['name']}: {u['current']} → {u['latest']} — {u['risk']}")
                await self._reply("\n".join(lines))
                from approval.broker import ApprovalBroker
                broker = ApprovalBroker(deliver=self.router.deliver_approval, notify=self.router.notify_resolved)
                return await self._reply(str((await self_update.propose_updates(broker)).get("status")))

            # -- approvals --
            if await self._maybe_resolve_approval(low):
                return

            # -- proactive tuning: "less of this" / "too many pings" --
            if re.search(r"\b(less of (this|these)|too many (pings|messages|notifications)|stop these)\b", low):
                from verticals.proactive import tune_down
                return await self._reply(tune_down())

            # -- relevance override: "ignore space X" / "space X is work" --
            m = re.match(r"^ignore\s+(?:space\s+)?(.+)$", t, re.I)
            if m:
                from learning import relevance
                relevance.mark(m.group(1).strip(), "personal")
                return await self._reply(f"👍 Ignoring '{m.group(1).strip()}' — it won't appear in briefings or triage.")
            m = re.match(r"^(?:space\s+)?(.+?)\s+is\s+work$", t, re.I)
            if m and len(m.group(1)) < 60:
                from learning import relevance
                relevance.mark(m.group(1).strip(), "work")
                return await self._reply(f"👍 Marked '{m.group(1).strip()}' as work-relevant.")

            # -- gchat: "watch space <name>" adds a group space to the bridge --
            m = re.match(r"^watch\s+(?:space\s+)?(.+)$", t, re.I)
            if m and len(m.group(1)) < 80:
                from conductor.loop import load_config, update_config
                watch = load_config().get("gchat_watch", [])
                name = m.group(1).strip()
                if name.lower() not in [w.lower() for w in watch]:
                    watch.append(name)
                    update_config(gchat_watch=watch)
                return await self._reply(f"👀 Watching GChat space '{name}' — trigger me "
                                         f"there with `@sentinel <ask>` (takes effect within ~25 min).")

            # -- model race on demand --
            m = re.match(r"^race\s+(.+)$", t, re.I)
            if m:
                return await self._race(m.group(1).strip())

            # -- ACP: steer a Claude Code coding session from chat --
            m = re.match(r"^code\s+on\s+(\S+/\S+)\s*[:\-]\s*(.+)$", t, re.I)
            if m:
                return await self._acp_code(m.group(1), m.group(2))
            m = re.match(r"^steer\s*[:\-]\s*(.+)$", t, re.I)
            if m:
                from channels import acp_steer
                ok = await acp_steer.steer("agent:main:main", m.group(1))
                return await self._reply("🎛️ Steered." if ok else "Couldn't steer — is a session running?")
            if low in ("cancel coding", "stop coding"):
                from channels import acp_steer
                ok = await acp_steer.cancel("agent:main:main")
                return await self._reply("🛑 Cancelled." if ok else "No live session to cancel.")

            # -- fix issue N: a real deterministic vertical, keep the fast path --
            m = re.match(r"^fix\s+issue\s+#?(\d+)(?:\s+(?:in|on)\s+(\S+/\S+))?", t, re.I)
            if m:
                return await self._fix_issue(int(m.group(1)), m.group(2))

            # -- EVERYTHING ELSE -> the agentic loop (no more regex intent-guessing).
            # It sees the FULL conversation + memory + work-context and chains real
            # tools (search email, download attachments, send files, run bash with
            # approval, reply, ...). This is what makes 'send me those payslips' and
            # continuous context actually work.
            return await self._agentic(t)
        except Exception as e:
            logger.warning("chat handle error: %s", e, exc_info=True)
            await self._reply(f"Hit an error handling that: {_safe_err(e)}")

    async def _agentic(self, text: str) -> None:
        """Delegate to the context-aware agentic loop (channels/agent_chat.py)."""
        await self._typing()
        from channels import agent_chat
        from approval.broker import ApprovalBroker
        broker = ApprovalBroker(deliver=self.router.deliver_approval,
                                notify=self.router.notify_resolved)
        transcript = self._convo.transcript()
        channel = self._convo.channel
        reply_via = self._reply_via
        # Multi-step tasks take a while → an EDIT-IN-PLACE progress bubble (one
        # message, edited on each milestone) instead of a dead "On it…". Only for
        # Telegram (edit API); other channels just get the final answer.
        tg = self._telegram if reply_via is None else None
        ack_id = await tg.send("🧠 On it…") if tg else None

        async def progress(msg: str):
            if tg and ack_id:
                await tg.edit_resolved(ack_id, "🧠 " + msg)

        async def _bg():
            try:
                # IDENTITY QUESTIONS ARE ANSWERED DETERMINISTICALLY (2026-08-14):
                # any LLM asked "are you Hermes or Claude?" reasons about its
                # MODEL identity — Claude's honesty training makes it say "I'm
                # Claude" no matter what the persona prompt claims, so the
                # answer flip-flopped between runs. Structural fix: identity
                # asks get the code-derived manifest, no model self-image
                # involved. (Boundaries by structure, never by prompt.)
                import re as _re
                _IDENTITY_RE = (
                    r"\b(are|r) (you|u)\b.*\b(hermes|claude|sentinel|gpt|model)\b|"
                    r"\b(is|was) (this|that|it)\b.*\b(hermes|claude|sentinel)\b|"
                    r"\byou\s*('re| are)\b.*\b(hermes|claude|sentinel)\b|"
                    r"\bstill (hermes|claude)\b|\b(hermes|claude( code)?) or (hermes|claude( code)?)\b|"
                    r"\bwho are you\b|\bwhat are you\b|\bwhich (model|engine|harness|brain)\b|"
                    r"\brunning on\b.*\b(hermes|claude|model)\b")
                if _re.search(_IDENTITY_RE, text, _re.I):
                    from conductor import introspect
                    ans = ("Deterministic answer (from the code manifest, not any model's "
                           "self-image):\n\n" + introspect.manifest_text("orchestration"))
                    from channels.conversation import Conversation
                    Conversation(channel).add("sentinel", ans)
                    if reply_via is not None:
                        await reply_via(ans)
                    elif tg and ack_id and await tg.edit_message(ack_id, ans):
                        pass
                    elif tg:
                        await tg.send(ans)
                    return
                # HERMES ORCHESTRATES CHAT (the owner, 2026-08-14): the harness
                # reasons over the conversation and spawns Claude Code
                # subagents through the bridge. The direct Claude loop below
                # is the automatic fallback when Hermes is unavailable/fails
                # — resilience, not a second brain.
                ans = None
                import os as _os
                if _os.getenv("SENTINEL_CHAT_ORCH", "hermes") == "hermes":
                    from brain import hermes_harness
                    from conductor import introspect
                    # broader self-questions (architecture/capabilities, any
                    # phrasing): hand Hermes the authoritative manifest so its
                    # answer is grounded in code facts, not model self-image.
                    self_block = (introspect.manifest_text(text)
                                  if introspect.is_self_question(text) else "")
                    await progress("orchestrating…")
                    ans = await hermes_harness.chat_turn(text, transcript,
                                                         self_knowledge=self_block)
                    if ans is None:
                        logger.warning("hermes chat orchestrator unavailable — direct fallback")
                        await progress("working (direct)…")
                if ans is None:
                    ans = await agent_chat.respond(text, channel, transcript,
                                                   telegram=self._telegram, broker=broker,
                                                   router=self.router, progress=progress)
            except Exception as e:
                logger.warning("agentic loop failed: %s", e, exc_info=True)
                agent_chat._record_failure(text, "loop", str(e))
                ans = f"Hit a snag working that: {_safe_err(e)}. I've logged it so I improve next time."
            # respond() can RETURN empty on a swallowed tool_error (not raise) —
            # never send blank (Telegram rejects it, so the owner sees silence and
            # thinks the agent is dead). Give an honest non-empty fallback.
            if not (ans or "").strip():
                agent_chat._record_failure(text, "empty_reply", "respond returned empty")
                ans = ("I couldn't produce a reply for that — a tool step failed "
                       "mid-turn and I've logged it. Try rephrasing, or ask again.")
            from channels.conversation import Conversation
            Conversation(channel).add("sentinel", ans)
            # gap-6: turn the '🧠 …' ack bubble INTO the answer (edit in place) instead of
            # leaving a stranded progress bubble above a new message. Fall back to a fresh
            # message if the edit fails or the answer is multi-chunk.
            if reply_via is not None:
                await reply_via(ans)
            elif tg and ack_id and len(ans) <= 4000 and await tg.edit_message(ack_id, ans):
                pass
            else:
                await self.router.broadcast(ans)
        asyncio.create_task(_bg())

    # ------------------------------------------------------------- built-ins

    async def _maybe_resolve_approval(self, low: str) -> bool:
        from approval import action_queue as aq
        pend = aq.get_pending_actions()
        # Only an id-shaped token counts as `approve <id>` — "approve of your
        # approach" must not fire _on_inbound with the garbage id "of" (and
        # "approve all" belongs to the vocabulary handling below).
        m = re.match(r"^/?(approve|reject)\s+((?:action-)?[0-9a-f]{6,})\b", low)
        if m:
            self.router._on_inbound(m.group(2), m.group(1), "telegram")
            if m.group(1) == "approve":
                self._note_strong_engagement()
            return True
        # bulk-approve confirmation: "approve all" first LISTS what it would
        # approve and waits for `confirm` — the pending queue mixes coach fixes
        # with sends/commands, and a blanket yes must never fire those blind.
        from conductor import kv as _kv
        if low in ("confirm", "confirmed", "confirm all"):
            bulk = _kv.get("bulk_approve") or {}
            fresh = False
            try:
                from datetime import datetime as _dt, timezone as _tz
                fresh = (_dt.now(_tz.utc) - _dt.fromisoformat(bulk.get("at", ""))
                         ).total_seconds() < 600
            except ValueError:
                pass
            _kv.put("bulk_approve", None)
            if fresh and bulk.get("ids"):
                pending_ids = {a["id"] for a in pend}
                for aid in bulk["ids"]:
                    if aid in pending_ids:
                        self.router._on_inbound(aid, "approve", "telegram")
                self._note_strong_engagement()
                return True
            await self._reply("Nothing awaiting a bulk confirm (it expires after "
                              "10 min) — say `tasks` to see what's pending.")
            return True

        exact_approve = low in ("approve", "yes", "ok", "go", "do it", "ship it", "approved")
        if exact_approve or (_resolution_intent(low, _APPROVE_WORDS, _APPROVE_STRONG)
                             and not (set(re.findall(r"[a-z']+", low)) & _REJECT_STRONG)):
            # "approve(d) all …" with several pending: list + one-word confirm
            if len(pend) > 1 and re.search(r"\b(all|everything|both|them)\b", low):
                from datetime import datetime as _dt, timezone as _tz
                _kv.put("bulk_approve", {"ids": [a["id"] for a in pend],
                                         "at": _dt.now(_tz.utc).isoformat()})
                lines = [f"That would approve ALL {len(pend)} pending actions:"]
                for a in pend:
                    lines.append(f"• `{a['id'].replace('action-','')}` "
                                 f"[{a.get('type','')}] {a.get('description','')[:60]}")
                lines.append("Reply `confirm` to approve every one, or "
                             "`approve <id>` for just one.")
                await self._reply("\n".join(lines))
                return True
            if len(pend) == 1:
                self.router._on_inbound(pend[0]["id"], "approve", "telegram")
                self._note_strong_engagement()   # B5: an APPROVAL is the real positive signal
                return True
            if len(pend) > 1:
                await self._reply(f"{len(pend)} things are waiting — say `approve <id>` "
                                  f"for the one you mean, or `approve all` (see /tasks).")
                return True
            # Nothing pending — DON'T feed "approved" to the planner as a new task
            # (that caused the promise-loop: it re-narrated the same intent). If the
            # last turn promised work, re-run it FOR REAL; else say nothing's queued.
            last_asks = [t["text"] for t in Conversation(self._convo.channel).recent(6)
                         if t["role"] == "user"]
            prior = last_asks[-2] if len(last_asks) >= 2 else ""
            if prior:
                await self._reply("Nothing was actually queued for approval — I'll just "
                                  "do it now for real.")
                await self._agentic(prior)       # execute the real task, not a narration
            else:
                await self._reply("Nothing's waiting on your approval right now. "
                                  "What do you want me to do?")
            return True
        if (low in ("reject", "no", "cancel", "stop")
                or _resolution_intent(low, _REJECT_WORDS, _REJECT_STRONG)):
            if len(pend) > 1 and re.search(r"\b(all|everything|both|them)\b", low):
                for a in pend:
                    self.router._on_inbound(a["id"], "reject", "telegram")
                return True
            if len(pend) == 1:
                self.router._on_inbound(pend[0]["id"], "reject", "telegram")
                return True
        return False

    def _note_strong_engagement(self) -> None:
        """An approval/act soon after a proactive ping is the genuine 'this was
        worth interrupting me' signal — the only thing that up-tunes a category."""
        try:
            from verticals.proactive import note_engagement
            note_engagement(strong=True)
        except Exception:
            pass

    async def _status(self, full: bool = False):
        from verticals.reporter import build_report
        await self._reply(build_report(24 if full else 12))

    async def _tasks(self):
        from approval import action_queue as aq
        pend = aq.get_pending_actions()
        lines = []
        if pend:
            lines.append(f"⏳ **{len(pend)} awaiting your approval:**")
            for a in pend[:8]:
                crit = "🔴" if a.get("criticality") == "critical" else "🟡"
                lines.append(f"{crit} `{a['id'].replace('action-','')}` — {a.get('description','')[:90]}")
            lines.append("Reply `approve <id>` / `reject <id>` (or just approve/reject if one).")
        else:
            lines.append("✅ Nothing waiting on you.")
        try:
            from conductor.loop import recent_work
            work = recent_work(hours=6)
            if work:
                lines.append(f"\nLast 6h: {len(work)} things done — /report for detail.")
        except Exception:
            pass
        await self._reply("\n".join(lines))

    async def _memory_summary(self):
        from memory import semantic
        s = semantic.stats()
        lines = [f"🧠 **{s['active']} facts** ({s['archived']} archived)"]
        for cat, n in sorted(s["by_category"].items()):
            lines.append(f"• {cat}: {n}")
        corrections = semantic.all_facts("correction")
        if corrections:
            lines.append("\nCorrections I will never repeat:")
            for c in corrections[:6]:
                lines.append(f"• {c['fact'][:100]}")
        prefs = semantic.all_facts("preference")[:4]
        if prefs:
            lines.append("\nTop preferences:")
            for p in prefs:
                lines.append(f"• {p['fact'][:100]}")
        lines.append("\nSay `remember <fact>` to teach me; corrections are automatic when you flag a mistake.")
        await self._reply("\n".join(lines))

    async def _models(self):
        try:
            from brain import arena
            await self._reply(arena.leaderboard_text())
        except Exception:
            await self._reply("No model races recorded yet — I race models on real tasks and keep evidence. Say `race <task>` to trigger one.")

    async def _insights(self):
        try:
            from learning import insights
            await self._reply(insights.digest())
        except Exception:
            await self._reply("No insights collected yet — they build up as I watch your work patterns.")

    async def _acp_code(self, repo: str, task: str):
        from channels import acp_steer
        if not acp_steer.available():
            return await self._reply("ACP steering needs OpenClaw (not available).")
        # strict repo validation — reject anything but owner/name word-chars, so no
        # shell metacharacters can reach the spawn path (defense in depth; the
        # executor is already no-shell/argv + cwd-confined).
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
            return await self._reply("Repo must look like owner/name (letters, digits, . _ - only).")
        import os
        name = repo.split("/")[-1]
        cand = os.path.expanduser(f"~/Projects/{name}")
        cwd = cand if os.path.isdir(cand) else os.path.expanduser("~/Projects")
        from approval.broker import ApprovalBroker
        broker = ApprovalBroker(deliver=self.router.deliver_approval, notify=self.router.notify_resolved)
        await self._reply(f"Proposing a Claude Code session on {repo} for: {task[:60]} — approve to start.")
        await broker.propose(**acp_steer.spawn_proposal(cwd, task))

    async def _race(self, task: str):
        await self._typing()
        await self._reply(f"🏁 Racing models on: {task[:70]} — give me a minute.")
        from brain import arena
        res = await arena.race(task, task_type="chat")
        if res.get("error"):
            return await self._reply(f"Race failed: {res['error'][:150]}")
        detail = res.get("verdict_detail") or {}
        losers = [r["model"] for r in res.get("results", [])
                  if r.get("model") != res.get("winner")]
        msg = (f"🏆 **{res.get('winner')}** won"
               + (f" vs {', '.join(losers)}" if losers else "")
               + (f" — {detail.get('reason','')}" if detail.get("reason") else "")
               + f"\n\n{(res.get('output') or '')[:2500]}")
        await self._reply(msg)

    # ----------------------------------------------------------------- files

    async def _send_file(self, spec: str):
        path = os.path.expanduser(spec)
        if not os.path.isabs(path):
            cand = os.path.join(str(Path.home()), spec)
            path = cand if os.path.exists(cand) else path
        rp = os.path.realpath(path)
        if not any(rp.startswith(os.path.realpath(r)) for r in _ALLOWED_READ_ROOTS):
            return await self._reply(f"I can only send files under {_ALLOWED_READ_ROOTS[0]}.")
        if any(mark in rp.lower() for mark in _BLOCKED_PATH_MARKERS):
            return await self._reply("That path looks sensitive (secrets/keys) — I won't send it.")
        if not os.path.isfile(rp):
            found = await self._find_file(spec)
            if found:
                rp = found
            else:
                return await self._reply(f"Couldn't find a file at: {spec}")
        if os.path.getsize(rp) > 45 * 1024 * 1024:
            return await self._reply("That file is over Telegram's ~50MB limit.")
        if self._telegram and await self._telegram.send_document(rp, caption=os.path.basename(rp)):
            self._convo.remember("last_file_sent", rp)
            self._convo.add("sentinel", f"[sent file {os.path.basename(rp)}]", kind="file")
        else:
            await self._reply(f"Tried to send {os.path.basename(rp)} but the upload failed.")

    async def _find_file(self, spec: str) -> str | None:
        """Fuzzy-find by name under home when the exact path misses."""
        name = os.path.basename(spec)
        try:
            proc = await asyncio.create_subprocess_exec(
                "mdfind", "-onlyin", str(Path.home()), f"kMDItemFSName == '{name}'",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            for line in out.decode().splitlines():
                lp = line.strip()
                if lp and os.path.isfile(lp) and not any(m in lp.lower() for m in _BLOCKED_PATH_MARKERS):
                    return lp
        except Exception:
            pass
        return None

    # ------------------------------------------------------------- workspace

    async def _check_email(self):
        await self._typing()
        from tools import gog_workspace as gw
        if not gw.available():
            return await self._reply("Gmail isn't available (gog CLI missing).")
        emails = await gw.search_emails("is:unread", 10)
        if not emails:
            return await self._reply("📭 No unread emails.")
        lines = [f"📧 **{len(emails)} unread:**"]
        stored = []
        for i, e in enumerate(emails[:10], 1):
            frm = (e.get("from", "") or "")[:45]
            subj = (e.get("subject", "(no subject)") or "")[:60]
            lines.append(f"{i}. {frm} — {subj}")
            stored.append({"n": i, "from": e.get("from", ""), "subject": e.get("subject", ""),
                           "id": e.get("id", e.get("threadId", ""))})
        self._convo.remember("last_emails", stored)
        lines.append("\nSay `reply to the 2nd one: <message>` and I'll draft it for your approval.")
        await self._reply("\n".join(lines))

    async def _check_chat(self):
        await self._typing()
        from tools import gog_workspace as gw
        if not gw.available():
            return await self._reply("Google Chat isn't available (gog CLI missing).")
        spaces = await gw.list_spaces(20)
        if not spaces:
            return await self._reply("No chat spaces found.")
        named = [s for s in spaces if s.get("name")]
        dm_count = len([s for s in spaces if s.get("type") == "DIRECT_MESSAGE"])
        lines = [f"💬 **{len(spaces)} chat spaces** ({dm_count} DMs):"]
        for s in named[:12]:
            lines.append(f"• {s.get('name')}")
        self._convo.remember("last_spaces", [s.get("name") for s in named[:12]])
        await self._reply("\n".join(lines))

    async def _whats_new(self):
        await self._typing()
        from tools import gog_workspace as gw
        snap = await gw.whats_new(24)
        emails = snap.get("unread_emails", [])
        lines = ["🗞️ **What you missed:**", f"📧 {len(emails)} unread email(s)"]
        stored = []
        for i, e in enumerate(emails[:5], 1):
            lines.append(f"{i}. {(e.get('from','') or '')[:35]}: {(e.get('subject','') or '')[:55]}")
            stored.append({"n": i, "from": e.get("from", ""), "subject": e.get("subject", "")})
        if stored:
            self._convo.remember("last_emails", stored)
        lines.append(f"💬 {len(snap.get('chat_spaces', []))} chat spaces")
        try:
            events = await gw.upcoming_events(5)
            if events:
                lines.append("📅 Next up:")
                for ev in events[:3]:
                    lines.append(f"• {(ev.get('summary','') or '')[:50]} — {(ev.get('start','') or '')[:16]}")
        except Exception:
            pass
        await self._reply("\n".join(lines))

    # ----------------------------------------------------------- send (gated)

    async def _send_email_proposal(self, to: str, message: str, subject_hint: str = ""):
        from approval.broker import ApprovalBroker
        subject = f"Re: {subject_hint}" if subject_hint else (message.split("\n")[0][:60] or "(reply)")
        broker = ApprovalBroker(deliver=self.router.deliver_approval, notify=self.router.notify_resolved)
        await self._reply(f"Drafting the email to {to} — approval coming.")
        await broker.propose(
            action_type="send_email",
            description=f"Send email to {to}: {subject}",
            details={"to": to, "subject": subject, "body": message},
            reasoning="Requested via chat.", project="comms",
            tier="A", reversibility="irreversible", criticality_hint="critical",
        )

    async def _send_gchat_proposal(self, person: str, message: str):
        from approval.broker import ApprovalBroker
        broker = ApprovalBroker(deliver=self.router.deliver_approval, notify=self.router.notify_resolved)
        await self._reply(f"Preparing the Google Chat message to {person} — approval coming.")
        await broker.propose(
            action_type="send_gchat",
            description=f"Send GChat to {person}: {message[:60]}",
            details={"person_name": person, "message": message},
            reasoning="Requested via chat.", project="comms",
            tier="A", reversibility="irreversible", criticality_hint="critical",
        )

    # ------------------------------------------------------------ code & web

    async def _research(self, question: str):
        await self._typing()
        await self._reply(f"🔎 On it — researching: {question[:80]}")
        from verticals import research_agent
        res = await research_agent.run_once(broker=None, question=question)
        ans = res.get("answer") or res.get("status") or "no answer"
        srcs = res.get("sources") or []
        msg = ans[:3500]
        if srcs:
            msg += "\n\nSources:\n" + "\n".join(f"• {s}" for s in srcs[:5])
        self._convo.remember("last_research", question)
        await self._reply(msg)

    async def _fix_issue(self, num: int, repo: str | None):
        from approval.broker import ApprovalBroker
        repos = self.config.get("github_repos", {})
        repo = repo or (next(iter(repos), None) if repos else None)
        if not repo or "/" not in repo:
            return await self._reply("Which repo? Say: fix issue 3 in owner/repo")
        owner, r = repo.split("/", 1)
        await self._reply(f"On it — working issue #{num} in {repo}. PR approval when ready.")
        broker = ApprovalBroker(deliver=self.router.deliver_approval, notify=self.router.notify_resolved)
        asyncio.create_task(self._run_specific_fix(owner, r, num, broker))

    async def _run_specific_fix(self, owner, repo, num, broker):
        from verticals import github_autopilot as ga
        try:
            perception = await ga.perceive(owner, repo)
            issue = next((i for i in perception.get("issues", []) if i.get("number") == num), None)
            if not issue:
                return await self._reply(f"Issue #{num} not found / not open in {owner}/{repo}.")
            fix = await ga.fix_issue(owner, repo, issue)
            if not fix.get("success"):
                return await self._reply(f"Couldn't produce a clean fix for #{num}: {fix.get('error','')[:200]}")
            from tools import test_runner
            tests = await test_runner.regression_check(fix.get("base_failures", []), fix["work_dir"])
            if tests.get("regression"):
                # self-heal: one Reflexion retry before giving up
                await self._reply(f"Fix for #{num} broke tests — trying once more with a reflection…")
                heal = await ga.reflexion_retry(owner, repo, issue, fix["work_dir"],
                                                str(tests.get("new_failures", "")))
                if not heal.get("recovered"):
                    return await self._reply(f"Still red after a retry on #{num} — stopping so I "
                                             f"don't thrash. {tests.get('new_failures')}")
                tests = heal.get("tests", tests)
                await self._reply(f"Recovered — #{num} is green now. Proposing the PR.")
            review = await ga.self_review(owner, repo, fix["work_dir"], fix.get("base", "main"))
            payload = ga.propose_pr_action(owner, repo, issue, fix, work_dir=fix["work_dir"], tests=tests, review=review)
            await broker.propose(**payload)
        except Exception as e:
            await self._reply(f"Error working #{num}: {e}")

    # ---------------------------------------------------------------- general

    async def _general(self, text: str):
        """Anything else → the orchestrator (army) for non-trivial work, or a
        single read-tier agent for trivial/conversational turns."""
        await self._typing()
        from conductor import introspect, orchestrator
        # Non-trivial, non-self-question work goes through the FULL army pipeline:
        # intake -> orchestrate (distribute+select) -> review -> observer -> report.
        # Trivial/conversational turns and self-questions stay on the fast inline
        # path below (they need the manifest + conversation context here).
        if not introspect.is_self_question(text) and not orchestrator.is_trivial(text):
            # The army pipeline (intake->orchestrate->review->observer) can take
            # ~30-90s. Don't block the chat: ack now, run in the background, reply
            # when done. Keeps the bot feeling responsive.
            from conductor import pipeline
            convo_snapshot = self._convo.transcript()
            channel = self._convo.channel
            await self._reply("🧠 On it — routing to the right agents, back in a moment.")

            async def _bg():
                try:
                    res = await pipeline.run(text, channel=channel, conversation=convo_snapshot)
                    ans = res.get("answer") or "I looked into it but came up empty — try rephrasing?"
                except Exception as e:
                    logger.warning("pipeline bg failed: %s", e)
                    ans = f"Hit an error working that: {e}"
                # reply on the same channel without re-entering handle()
                from channels.conversation import Conversation
                Conversation(channel).add("sentinel", ans)
                if self._reply_via is not None:
                    await self._reply_via(ans)
                else:
                    await self.router.broadcast(ans)
            asyncio.create_task(_bg())
            return
        from brain import claude_runner
        from memory import semantic
        memory_block = semantic.prompt_block(context_hint=text)
        transcript = self._convo.transcript()
        slots = self._convo.slots_block()
        # If he's asking about Sentinel itself, ground the answer in real
        # architecture facts (read from the codebase) — never guess.
        self_block = ""
        if introspect.is_self_question(text):
            self_block = introspect.manifest_text(text) + "\n\n"
        prompt = (
            "You are Sentinel, the owner's personal AI chief-of-staff, replying in his "
            "chat. This is an ongoing conversation — use its context; follow-up "
            "messages refer to earlier ones.\n\n"
            f"{VOICE}\n\n"
            f"{self_block}"
            f"{memory_block}\n\n"
            f"Recent conversation:\n{transcript}\n\n"
            f"{slots}\n\n"
            "You may read files and run read-only commands on this Mac to find "
            "answers. When asked about yourself/your architecture, use the "
            "self-knowledge block above and you may read your own source under "
            f"{introspect.REPO_ROOT} — do NOT answer from general knowledge. You "
            "CANNOT send/modify/delete anything from this session — if the owner wants "
            "an action, tell him the exact phrase (e.g. 'send me <path>', 'fix "
            "issue N in owner/repo', 'reply to <email>: <msg>').\n\n"
            f"the owner's new message: {text}\n\nReply as Sentinel now:"
        )
        out = ""
        async for ev in claude_runner.run_claude(
            prompt, agent="conductor", tier="read",
            allowed_tools=["Read", "Grep", "Glob", "Bash"], model=None, max_turns=0,
        ):
            if ev.type == "result":
                out = ev.content or ""
        await self._reply(out or "I didn't get a response — try rephrasing?")

    # ---------------------------------------------------------------- helpers

    async def _reply(self, text: str):
        self._convo.add("sentinel", text)
        if self._reply_via is not None:
            try:
                await self._reply_via(text)
                return
            except Exception as e:
                logger.warning("reply_via failed, falling back to broadcast: %s", e)
        await self.router.broadcast(text)

    async def _typing(self):
        if self._telegram and self._reply_via is None:
            await self._telegram.typing()


def _parse_hours(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hour|hours|m|min|minutes?|d|day|days)?", text)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "h")[0]
    return val / 60 if unit == "m" else val * 24 if unit == "d" else val


def _extract_email(from_field: str) -> str | None:
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_field or "")
    return m.group(0) if m else None


def _safe_err(e) -> str:
    """Scrub secret-shaped text from an exception before echoing it to chat —
    an auth error could embed a token; strip it (goes to the owner + is persisted)."""
    try:
        from memory.gateway import _scrub
        return _scrub(str(e))[:200]
    except Exception:
        return str(e)[:200]
