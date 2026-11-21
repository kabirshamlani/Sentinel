"""Sentinel daemon — the single long-lived process. Owns channels + all clocks.

Besides durable approvals (sweep of expired routine windows), the daemon now
runs Sentinel's entire internal cron — no launchd needed for any of it:

  every 60s   approval sweep (expired routine windows -> best-effort + report)
  every 30m   ambient observer refresh + proactive cycle (interrupt budget 3/day)
  every 3h    heartbeat (Needs-you / Done / Watching; skip-if-nothing; quiet hours)
  every 2h    autonomous work cycle (github issues -> fixes -> PR approvals),
              idle-gated so it never fights the owner for the machine
  ~06:05      morning market briefing (images + digest + gated TI-ENGG proposal)
  ~03:30      nightly memory consolidation (dream + brainlift + decay)
  when idle   one experiment (model race etc.), max 2/day
  continuous  Telegram long-poll + GChat bridge poll
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.paths import ensure_home
from approval import action_queue as aq
from approval import broker as bk
from conductor import kv

logger = logging.getLogger("daemon")

SWEEP_INTERVAL = 60  # seconds

_ROUTER = None       # module-level so verticals can reach the live router
_BROKER = None       # the live approval broker (verticals propose through it)


def get_router():
    return _ROUTER


def get_broker():
    return _BROKER


def _expired(action: dict) -> bool:
    exp = action.get("window_expires_at")
    if not exp:
        return False
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(exp)
    except ValueError:
        return False


class SentinelDaemon:
    def __init__(self):
        self.router = None
        self._telegram = None
        self._gchat = None
        self._running_jobs: set[str] = set()
        self._boot_sha = ""          # HEAD at boot; a moved HEAD => restart to load it
        self._boot_at = datetime.now(timezone.utc)

    async def build(self):
        global _ROUTER, _BROKER
        ensure_home()
        from channels.telegram import TelegramChannel
        from channels.openclaw_bridge import OpenClawChannel
        from channels.router import ChannelRouter
        import os

        self._telegram = TelegramChannel()
        wa_to = os.getenv("SENTINEL_WHATSAPP_TO", "")
        openclaw = [OpenClawChannel("whatsapp", wa_to)] if wa_to else []
        self.broker = bk.ApprovalBroker()
        self.router = ChannelRouter(self.broker, telegram=self._telegram, openclaw_channels=openclaw)
        self.broker._deliver = self.router.deliver_approval
        self.broker._notify = self.router.notify_resolved
        self.router.set_correction_handler(self._on_correction)
        from channels.chat_handler import ChatHandler
        from conductor.loop import load_config
        self.config = load_config()
        chat = ChatHandler(self.router, config=self.config)
        chat.bind_telegram(self._telegram)
        self.router.set_chat_handler(chat)
        self.router._broker = _PersistentResolver(self.router)
        # GChat bridge (the owner-triggered bot in Google Chat)
        from channels.gchat_bridge import GChatBridge
        self._gchat = GChatBridge(chat, config=self.config)
        # unified memory: migrate legacy scattered stores into the single gateway
        # (idempotent), then seed MEMORY.md/BRAINLIFT.md views.
        try:
            from memory import migrate
            migrate.run()
        except Exception as e:
            logger.warning("memory migration skipped: %s", e)
        from memory.consolidate import ensure_seeds
        ensure_seeds()
        # sync external stores (OpenClaw/Hermes) into the canonical gateway
        try:
            from memory import sync
            await sync.sync_all()
        except Exception as e:
            logger.debug("memory sync skipped: %s", e)
        _ROUTER = self.router
        _BROKER = self.broker
        return self

    def _on_correction(self, text: str, channel: str):
        """Correction -> RCA never-repeat rule + a SACRED semantic fact."""
        try:
            from learning.rca import handle_inbound_correction
            from conductor.loop import recent_work, log_work
            res = handle_inbound_correction(text, lambda: recent_work(hours=24))
            log_work("rca", f"correction: {text[:80]}", "logged", res)
            try:
                from memory import semantic
                semantic.add_fact(f"Correction ({datetime.now().date()}): {text.strip()[:300]}",
                                  category="correction", source="owner", confidence=1.0)
            except Exception:
                pass
            # a correction demotes recently-autonomous actions — trust is earned
            # slowly and lost instantly (research doc 11 §f: self-report is poison)
            try:
                from approval import autonomy, action_queue as _aq
                for a in _aq.get_actions("executed")[:3]:
                    autonomy.record_failure(a.get("type", ""), a.get("project", ""),
                                            reason=f"correction: {text[:80]}")
            except Exception:
                pass
            asyncio.create_task(self.router.broadcast(
                "🧠 Logged that correction — added a never-repeat rule and stored it "
                "in permanent memory."))
        except Exception as e:
            logger.warning("correction handling failed: %s", e)

    # ------------------------------------------------------------- approvals

    async def sweep_once(self):
        for act in aq.get_pending_actions():
            if act.get("criticality") == "critical":
                continue
            if _expired(act):
                logger.info("Sweep: window expired for %s (%s)", act["id"], act.get("type"))
                aq.approve_action(act["id"], resolved_by="timeout_best_effort")
                plan = act.get("best_effort_plan") or act.get("description", "")
                await self.router.broadcast(
                    f"⏱️ Approval window (30 min) expired — went ahead with the best "
                    f"approach:\n• {act.get('description','')}\n• {plan}\n(Reply to undo if wrong.)")

    # ---------------------------------------------------------- internal cron

    def _due(self, name: str, seconds: float) -> bool:
        stamps = kv.get("cron_stamps", {}) or {}
        last = stamps.get(name)
        if last:
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < seconds:
                    return False
            except ValueError:
                pass
        stamps[name] = datetime.now(timezone.utc).isoformat()
        kv.put("cron_stamps", stamps)
        return True

    def _daily_due(self, name: str) -> bool:
        stamps = kv.get("cron_daily", {}) or {}
        today = datetime.now().date().isoformat()
        if stamps.get(name) == today:
            return False
        stamps[name] = today
        kv.put("cron_daily", stamps)
        return True

    def _spawn(self, name: str, coro):
        """Run a job exactly-once-at-a-time, errors contained."""
        if name in self._running_jobs:
            return

        async def guarded():
            self._running_jobs.add(name)
            try:
                await coro
            except Exception as e:
                logger.warning("job %s failed: %s", name, e)
            finally:
                self._running_jobs.discard(name)
        asyncio.create_task(guarded())

    async def cron_tick(self):
        now = datetime.now()  # local time for wall-clock jobs

        # BRAIN CANARY (hourly): a 1-token live auth probe through the same
        # gateway every agent uses. RCA 2026-08-14: every LLM call 401'd for
        # ~36h (expired JWT) while logs recorded "success" — skip-if-nothing
        # made decapitation look like health. The canary alerts LOUDLY and
        # keeps re-alerting every 4h until auth recovers.
        if self._due("brain_canary", 3600):
            self._spawn("brain_canary", self._brain_canary())

        # MISSION ENGINE (every 10 min, NEVER idle-gated): perceive email/chat
        # → correlate on the timeline → judge → queue missions. The owner
        # 2026-08-14: "the agent should start working as soon as it gets the
        # task" — and "I want Hermes as the main harness": the JUDGE is Hermes
        # (profile `sentinel`, its own cron, the MCP bridge as hands); the
        # in-process judge remains only as the fallback when Hermes is broken.
        if self._due("mission_scan", 600):
            self._spawn("mission_scan", self._mission_scan())

        # MISSION EXECUTOR (every 60s): start queued missions (Claude Code in
        # isolated worktrees, ≤2 concurrent). The daemon is the one executor
        # host — a mission survives whichever brain queued it exiting.
        if self._due("mission_exec", 60):
            self._spawn("mission_exec", self._mission_exec())

        # observer refresh + proactive engine (every 30 min)
        if self._due("observer", 1800):
            from tools.observer import build_work_context
            self._spawn("observer", build_work_context())
        if self._due("proactive", 1800):
            from verticals.proactive import run_cycle
            self._spawn("proactive", run_cycle(self.router))

        # heartbeat (module decides due/skip/quiet itself)
        from verticals import heartbeat as hb
        if hb.due() and "heartbeat" not in self._running_jobs:
            self._spawn("heartbeat", self._heartbeat())

        # morning briefing ~06:05 local
        if now.hour == 6 and now.minute >= 5 and self._daily_due("briefing"):
            from verticals.market_brief import run as brief_run
            self._spawn("briefing", brief_run(self.router, self._telegram))

        # rebuild today.md (the "understand my day" working memory) each morning
        # ~06:20, and seed goals.yaml once (inferred) if it doesn't exist yet.
        if now.hour == 6 and now.minute >= 20 and self._daily_due("build_today"):
            self._spawn("build_today", self._build_day())

        # nightly consolidation ~03:30 local
        if now.hour == 3 and now.minute >= 30 and self._daily_due("consolidation"):
            from memory.consolidate import nightly
            self._spawn("consolidation", nightly())

        # autonomous work cycle every 2h, idle-gated + budget-gated, if repos configured
        if self.config.get("github_repos") and self._due("work_cycle", 7200):
            try:
                from conductor.scheduler import is_free_for_heavy_work
                free, why = is_free_for_heavy_work()
            except Exception:
                free, why = True, "gate unavailable"
            if free and self._budget_ok():
                self._spawn("work_cycle", self._work_cycle())
            else:
                logger.info("work cycle skipped: %s", why if not free else "daily budget spent")

        # idle experiments (cap 2/day) — discretionary, so ALSO budget-gated
        if self._due("experiment_check", 5400):
            from conductor import experiments
            if experiments.can_run() and not aq.get_pending_actions() and self._budget_ok():
                try:
                    from conductor.scheduler import is_free_for_heavy_work
                    free, _ = is_free_for_heavy_work()
                except Exception:
                    free = False
                if free:
                    self._spawn("experiment", experiments.run_next(self.router))

        # eval-gated self-evolution (weekly, idle+budget-gated — Wed ~10:00)
        if now.weekday() == 2 and now.hour == 10 and self._daily_due("self_evolve"):
            try:
                from conductor.scheduler import is_free_for_heavy_work
                free, _ = is_free_for_heavy_work()
            except Exception:
                free = False
            if free and self._budget_ok():
                self._spawn("self_evolve", self._self_evolve())

        # weekly: check for failing agents -> propose repair (Mon ~10:00)
        if now.weekday() == 0 and now.hour == 10 and self._daily_due("agent_repair"):
            self._spawn("agent_repair", self._agent_repair())

        # self-improvement: scan failure clusters -> guardrail / propose specialist
        # (daily ~11:00, bounded; only acts on a demonstrated recurring failure)
        if now.hour == 11 and self._daily_due("self_improve"):
            self._spawn("self_improve", self._self_improve())

        # observer sweep: watch the agents, escalate degraded ones (every ~2h)
        if self._due("observer_sweep", 7200):
            from conductor import observer
            self._spawn("observer_sweep", observer.sweep(self.router))

        # coaching review: grade every agent's real episodes against its
        # definition-of-good, RCA the failures, file kanban issues (daily
        # ~12:05 — one hour after self_improve so the coach grades that run too)
        if now.hour == 12 and now.minute >= 5 and self._daily_due("coach_review"):
            self._spawn("coach_review", self._coach_review())

        # PM autopilot: review pending coach fix PRs (independent PM agent +
        # deterministic gates), auto-approve after the owner's 3h silence window,
        # bounded + self-disabling on any human disagreement. Every ~15 min.
        if self._due("pm_sweep", 900):
            self._spawn("pm_sweep", self._pm_sweep())

        # self-restart: when merged code has landed (PM autopilot or a manual
        # approval), re-exec so the fix actually runs. Only between jobs.
        if self._restart_needed() and not self._running_jobs:
            await self._graceful_restart()

        # LIVE-FAILURE ESCALATION (the owner 2026-08-15): a failed real task filed
        # a coaching issue and flagged the fixer — run it at THIS tick, not
        # the 2h schedule, and not idle-gated (self-repair of a broken task
        # path outranks staying out of the way). Budget still applies.
        if kv.get("coach_fix_asap") and "coach_fix" not in self._running_jobs:
            if self._budget_ok():
                kv.put("coach_fix_asap", "")
                logger.info("coach fix ASAP: live failure escalated — running now")
                self._spawn("coach_fix", self._coach_fix())

        # coaching fix cycle: pick the top kanban issue -> repro -> fix branch
        # -> verified before/after -> PR proposal (critical, held for the owner).
        # Every 2h, idle+budget-gated like the work cycle (it spawns coding agents).
        if self._due("coach_fix", 7200):
            try:
                from conductor.scheduler import is_free_for_heavy_work
                free, why = is_free_for_heavy_work()
            except Exception:
                free, why = True, "gate unavailable"
            if free and self._budget_ok():
                self._spawn("coach_fix", self._coach_fix())
            else:
                logger.info("coach fix skipped: %s", why if not free else "daily budget spent")

        # self-update: check runtime/tool versions weekly -> propose upgrades
        # (critical approval, rollback-able; never silent — Tue ~10:00)
        if now.weekday() == 1 and now.hour == 10 and self._daily_due("self_update"):
            self._spawn("self_update", self._self_update())

        # re-sync external memory (OpenClaw/Hermes) into the canonical store (6h)
        if self._due("mem_sync", 21600):
            from memory import sync
            self._spawn("mem_sync", sync.sync_all())

        # refresh the people roster (name↔email↔DM-space) from Gmail+Calendar (6h)
        # so the owner can refer to anyone by name and DM summaries show real names.
        if self._due("people_roster", 21600):
            from tools import people
            self._spawn("people_roster", people.build_roster())

    async def _heartbeat(self):
        from verticals import heartbeat as hb
        text = await hb.build()
        hb.mark_sent()                    # silence counts as a beat
        if text:
            await self.router.broadcast(text)

    async def _mission_scan(self):
        import os
        from verticals import missions
        ingested = await missions.perceive()
        if os.getenv("SENTINEL_HARNESS", "hermes") == "hermes":
            from brain import hermes_harness
            if await hermes_harness.tick():
                logger.info("mission scan: hermes harness tick ok (ingested %d)", ingested)
                return
            logger.warning("hermes harness unavailable/failed — internal fallback judge")
        res = await missions.scan(self.broker, self.router)
        if res.get("started"):
            logger.info("mission scan (fallback): queued %s (ingested %d events)",
                        res["started"], ingested)

    async def _mission_exec(self):
        from verticals import missions
        n = await missions.run_queued(self.broker, self.router)
        if n:
            logger.info("mission exec: started %d queued mission(s)", n)

    async def _brain_canary(self):
        """Prove the LLM path is alive; scream (and keep screaming) if not."""
        from brain import providers, gateway_auth

        async def _probe() -> str | None:
            r = await providers.call_model("claude-haiku-4-5",
                                           "Reply with the single word: ok",
                                           max_tokens=8, timeout=45)
            err = r.get("error")
            return None if (not err and (r.get("text") or "").strip()) else (err or "empty response")

        err = await _probe()
        if err:                                    # one forced re-resolve + retry
            gateway_auth.invalidate()
            err = await _probe()

        now_iso = datetime.now(timezone.utc).isoformat()
        state = kv.get("brain_canary", {}) or {}

        # Secondary detector: many runs but zero recorded cost = every spawn is
        # failing upstream of usage accounting (the exact Aug-13 signature:
        # 47 runs, $0.00). Catches CLI-path death even when the raw-API probe
        # above still works.
        ledger = kv.get("cost_ledger", {}) or {}
        zero_cost = (ledger.get("runs", 0) >= 15 and float(ledger.get("cost_usd", 0) or 0) == 0.0)

        if not err and not zero_cost:
            if state.get("failing_since"):
                await self.router.broadcast(
                    "🟢 Brain recovered — LLM auth is working again "
                    f"(was failing since {state['failing_since'][:16]}).")
            kv.put("brain_canary", {"failing_since": None, "last_ok": now_iso})
            return

        reason = err or f"{ledger.get('runs', 0)} runs today with $0.00 recorded (every spawn failing)"
        failing_since = state.get("failing_since") or now_iso
        last_alert = state.get("last_alert")
        alert_due = True
        if last_alert:
            try:
                alert_due = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(last_alert)).total_seconds() > 4 * 3600
            except ValueError:
                pass
        if alert_due:
            state["last_alert"] = now_iso
            await self.router.broadcast(
                "🔴 BRAIN DOWN — LLM calls are failing, so triage/drafting/meeting "
                f"analysis are all dead until this is fixed.\n• Since: {failing_since[:16]}\n"
                f"• Error: {str(reason)[:180]}\n"
                "• Fix: run `tfy login --host <your TrueFoundry host>` "
                "or refresh the keychain item `tfy-gateway-pat`.")
        state["failing_since"] = failing_since
        kv.put("brain_canary", state)

    async def _work_cycle(self):
        from conductor.loop import Conductor
        # reuse the live broker/router so approvals hit the running channels
        c = Conductor(self.broker, self.router, self.config)
        await c.run_full_cycle()

    async def _agent_repair(self):
        from learning import agent_factory
        for slug in agent_factory.failing_agents()[:2]:
            proposal = await agent_factory.draft_repair(slug)
            if proposal:
                await self.broker.propose(**proposal)

    async def _self_improve(self):
        from learning import self_improve
        res = await self_improve.propose_improvement(self.broker)
        logger.info("self-improve: %s", res)

    async def _coach_review(self):
        from coaching import coach
        res = await coach.run_coaching(self.broker, self.router, since_hours=24)
        logger.info("coach review: %d episodes, %d issues filed, board %s",
                    res.get("episodes_reviewed", 0),
                    len(res.get("issues_filed", [])), res.get("board"))

    async def _coach_fix(self):
        from coaching import fixer
        res = await fixer.run_fix_cycle(self.broker, max_issues=1)
        logger.info("coach fix: %s", res)

    async def _pm_sweep(self):
        from coaching import pm
        res = await pm.sweep(self.router)
        if res.get("reviewed") or res.get("auto_approved"):
            logger.info("pm sweep: %s", res)

    # ------------------------------------------------------------- restart

    def _restart_needed(self) -> bool:
        """True when merged code has landed since this process booted —
        either the PM flagged it, or HEAD moved and then stayed STABLE for
        5+ minutes (debounce: someone actively committing must not trigger a
        restart storm), with 10+ minutes of uptime (no boot loops)."""
        if kv.get("restart_required"):
            return True
        if (datetime.now(timezone.utc) - self._boot_at).total_seconds() < 600:
            return False
        try:
            import subprocess
            from config.paths import REPO_ROOT
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                                  capture_output=True, text=True, timeout=10).stdout.strip()
            if not (self._boot_sha and head) or head == self._boot_sha:
                kv.put("head_seen", None)
                return False
            seen = kv.get("head_seen") or {}
            if seen.get("sha") != head:
                kv.put("head_seen", {"sha": head, "at": datetime.now(timezone.utc).isoformat()})
                return False
            first = datetime.fromisoformat(seen["at"])
            return (datetime.now(timezone.utc) - first).total_seconds() >= 300
        except Exception:
            return False

    async def _graceful_restart(self):
        """Announce, then replace this process with a fresh one (same argv
        semantics as the manual start). State is externalized by design, so
        nothing is lost; approvals and cron stamps live on disk."""
        import os, sys
        from config.paths import REPO_ROOT
        reason = (kv.get("restart_required") or {}).get("reason", "new code on HEAD")
        kv.put("restart_required", None)
        logger.info("Restarting daemon: %s", reason)
        try:
            await self.router.broadcast(
                f"♻️ Restarting to load merged fixes ({reason}) — back in ~10s.")
        except Exception:
            pass
        os.chdir(str(REPO_ROOT))
        os.execv(sys.executable, [sys.executable, "-m", "conductor.daemon"])

    async def _self_update(self):
        from learning import self_update
        res = await self_update.propose_updates(self.broker)
        logger.info("self-update: %s", res)

    async def _build_day(self):
        """Morning: seed goals.yaml (once, inferred → the owner corrects) + rebuild today.md."""
        from verticals import day
        if not day.GOALS.exists():
            try:
                drafted = await day.infer_goals()
                if drafted:
                    await self.router.broadcast(
                        "Drafted your goals from recent work — correct me if it's off:\n\n"
                        + drafted[:800] + "\n\n(reply 'goals: <your version>' to fix)")
            except Exception as e:
                logger.debug("goals inference skipped: %s", e)
        try:
            await day.build_today()
        except Exception as e:
            logger.debug("build_today skipped: %s", e)

    async def _self_evolve(self):
        from learning import evolve
        try:
            res = await evolve.run_evolution(self.broker)
            logger.info("self-evolve: %s", res)
        except Exception as e:
            logger.warning("self-evolve failed: %s", e)

    # B3: daily spend ceiling. Discretionary jobs (experiments, work-cycle,
    # evolution) are skipped once the day's Claude spend passes the ceiling, so a
    # runaway loop can't burn the budget. Approvals/chat/heartbeat are NOT gated.
    DAILY_BUDGET_USD = 15.0

    def _budget_ok(self) -> bool:
        from datetime import datetime
        led = kv.get("cost_ledger", {}) or {}
        if led.get("date") != datetime.now().date().isoformat():
            return True
        return led.get("cost_usd", 0.0) < self.DAILY_BUDGET_USD

    # ------------------------------------------------------------------- run

    async def run(self):
        await self.build()
        try:
            import subprocess
            from config.paths import REPO_ROOT
            self._boot_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            self._boot_sha = ""
        listeners = await self._start_listeners()
        logger.info("Daemon up. channels=%s gchat=%s", self.router.configured_names or "(none)",
                    bool(self._gchat))
        if self._telegram and self._telegram.configured:
            await self._telegram.set_commands()
        try:
            while True:
                try:
                    await self.sweep_once()
                except Exception as e:
                    logger.warning("sweep error: %s", e)
                try:
                    await self.cron_tick()
                except Exception as e:
                    logger.warning("cron error: %s", e)
                await asyncio.sleep(SWEEP_INTERVAL)
        finally:
            for t in listeners:
                t.cancel()

    async def _start_listeners(self):
        tasks = []
        if self._telegram and self._telegram.configured:
            tasks.append(asyncio.create_task(self._telegram.start_polling()))
        if self._gchat is not None:
            from tools import gog_workspace as gw
            if gw.available():
                tasks.append(asyncio.create_task(self._gchat.run()))
        # OpenClaw INBOUND (Wave 3): start a receive loop per OpenClaw channel so the owner
        # can COMMAND Sentinel from WhatsApp/iMessage — not just receive approvals. Each
        # loop no-ops safely until its channel is QR-linked. _on_message was wired by the
        # router; this is what finally CALLS it (it was dead code before).
        for ch in getattr(self.router, "openclaw_channels", []) or []:
            if hasattr(ch, "run_receive_loop") and ch.configured:
                tasks.append(asyncio.create_task(ch.run_receive_loop()))
        return tasks


class _PersistentResolver:
    """Resolve() that writes directly to the persisted action queue, so a button
    tap works even though the proposing process has long exited."""

    def __init__(self, router):
        self._router = router

    def resolve(self, action_id: str, decision: str, channel: str = "telegram") -> bool:
        if not action_id:
            return False
        if not action_id.startswith("action-"):
            action_id = f"action-{action_id}"
        act = aq.get_action(action_id)
        if not act or act.get("status") != "pending":
            return False
        from approval import autonomy
        if decision == "approve":
            done = aq.approve_action(action_id, resolved_by=f"user:{channel}")
            if act.get("project"):
                bk.grant_project(act["project"], act["type"], action_id)
            # evidence toward earned autonomy — only human approvals count, and
            # only if execution actually succeeded
            if done and done.get("status") == "executed":
                autonomy.record_success(act["type"], act.get("project", ""),
                                        resolved_by=f"user:{channel}")
            asyncio.create_task(self._router.broadcast(
                f"✅ Approved & done: {act.get('description','')}"))
            return bool(done)
        else:
            aq.reject_action(action_id, reason=f"rejected via {channel}", resolved_by=f"user:{channel}")
            # a rejection is a demotion signal — revoke any earned autonomy
            autonomy.record_failure(act["type"], act.get("project", ""),
                                    reason="rejected by user")
            asyncio.create_task(self._router.broadcast(
                f"❌ Rejected (won't retry): {act.get('description','')}"))
            return True


def main():
    import logging as _l, os
    _l.basicConfig(level=os.getenv("SENTINEL_LOG", "INFO"),
                   format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs the full request URL at INFO, which for Telegram embeds the bot
    # token in the path (/bot<TOKEN>/…) — a secret leak into the logfile. Quiet it.
    for noisy in ("httpx", "httpcore"):
        _l.getLogger(noisy).setLevel(_l.WARNING)
    asyncio.run(SentinelDaemon().run())


if __name__ == "__main__":
    main()
