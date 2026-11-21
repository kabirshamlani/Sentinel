"""Conductor — the top-level loop owner.

Owns perceive -> reason -> propose -> act -> report -> learn. Deterministic,
no LLM at this layer; it dispatches reasoning to Claude Code via the verticals
and gates every mutation through the approval broker. Config comes from
config/sentinel.toml-like settings loaded from SENTINEL_HOME/config.json (or
sensible defaults).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from config.paths import SENTINEL_HOME, WORK_LOG_PATH, ensure_home
from approval.broker import ApprovalBroker

logger = logging.getLogger("conductor")

CONFIG_PATH = SENTINEL_HOME / "config.json"

DEFAULT_CONFIG = {
    # repos the GitHub autopilot watches: {"owner/repo": "/path/to/local/clone"}
    "github_repos": {},
    # whether each vertical is enabled
    "verticals": {"github": True, "inbox": True, "meeting": True},
    # report cadence handled by launchd; this just formats
    "report_channels": ["telegram", "whatsapp"],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def update_config(**fields) -> dict:
    """Persist config changes (e.g. chat command 'watch space X')."""
    on_disk = {}
    if CONFIG_PATH.exists():
        try:
            on_disk = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    on_disk.update(fields)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(on_disk, indent=2, ensure_ascii=False))
    return load_config()


def log_work(kind: str, summary: str, status: str, detail: dict | None = None):
    WORK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind,
             "summary": summary, "status": status, "detail": detail or {}}
    with open(WORK_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def recent_work(hours: int = 24) -> list[dict]:
    if not WORK_LOG_PATH.exists():
        return []
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for line in WORK_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) > cutoff:
                out.append(e)
        except Exception:
            continue
    return out


class Conductor:
    def __init__(self, broker: ApprovalBroker, router, config: dict):
        self.broker = broker
        self.router = router
        self.config = config

    @classmethod
    async def build(cls) -> "Conductor":
        ensure_home()
        config = load_config()
        # channels
        from channels.telegram import TelegramChannel
        from channels.openclaw_bridge import OpenClawChannel
        from channels.router import ChannelRouter

        tg = TelegramChannel()
        wa_to = os.getenv("SENTINEL_WHATSAPP_TO", "")
        openclaw = [OpenClawChannel("whatsapp", wa_to)] if wa_to else []

        broker = ApprovalBroker()
        router = ChannelRouter(broker, telegram=tg, openclaw_channels=openclaw)
        # wire broker delivery + notify to the router
        broker._deliver = router.deliver_approval
        broker._notify = router.notify_resolved
        c = cls(broker, router, config)
        c._telegram = tg
        # wire "you made a mistake" corrections -> RCA
        router.set_correction_handler(c._handle_correction)
        return c

    def _handle_correction(self, text: str, channel: str):
        """User pointed out a mistake -> open an RCA (store, root-cause, never
        repeat) and, if a compensating action is obvious, propose it as critical."""
        try:
            from learning.rca import handle_inbound_correction
            result = handle_inbound_correction(text, lambda: recent_work(hours=24))
            comp = result.get("compensating_action")
            if comp:
                asyncio.create_task(self.broker.propose(
                    **comp, criticality_hint="critical", reversibility="reversible",
                    project=comp.get("project", "correction")))
            log_work("rca", f"correction: {text[:80]}", "logged", result)
        except Exception as e:
            logger.warning("RCA handling failed: %s", e)

    async def start_channel_listeners(self):
        """Start Telegram long-poll (and any inbound listeners) as background tasks."""
        tasks = []
        if getattr(self, "_telegram", None) and self._telegram.configured:
            tasks.append(asyncio.create_task(self._telegram.start_polling()))
        return tasks

    # ---- cycle entrypoints ----

    async def run_full_cycle(self):
        # Cycles propose non-blocking (broker returns 'awaiting_approval' and
        # persists window_expires_at); the long-lived daemon owns channel
        # listeners + the expiry sweeper, so approvals survive this process
        # exiting (gap P0-1). No listeners needed here.
        await self._run_verticals(heavy=True)

    async def run_perception_only(self):
        await self._run_verticals(heavy=False)

    async def _run_verticals(self, heavy: bool):
        tasks = []
        v = self.config["verticals"]
        if v.get("github") and heavy:
            tasks.append(self._run_github())
        if v.get("inbox"):
            tasks.append(self._run_inbox(heavy))
        if v.get("meeting"):
            tasks.append(self._run_meeting())
        if not tasks:
            logger.info("No verticals enabled/eligible this cycle")
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("vertical error: %s", r)

    async def _run_github(self):
        from verticals import github_autopilot as ga
        # github_repos is now just a list of "owner/repo" (worktrees are managed
        # by repo_manager) — also accept the legacy {repo: path} dict form.
        repos = self.config.get("github_repos", {})
        repo_keys = list(repos.keys()) if isinstance(repos, dict) else list(repos)
        # work repos in parallel (each gets an isolated worktree)
        async def _one(repo_key):
            owner, repo = repo_key.split("/", 1)
            try:
                res = await ga.run_once(owner, repo, self.broker)
                log_work("github", f"{repo_key}: {res.get('status')}", res.get("status", "?"), res)
            except Exception as e:
                log_work("github", f"{repo_key} failed", "error", {"error": str(e)})
        if repo_keys:
            await asyncio.gather(*[_one(k) for k in repo_keys], return_exceptions=True)

    async def _run_inbox(self, heavy: bool):
        try:
            from verticals import inbox_triage
        except Exception:
            return
        try:
            res = await inbox_triage.run_once(self.broker, propose=heavy)
            nc = res.get("notify_count", 0)
            summary = (f"triaged {res.get('count', 0)} items · "
                       f"{res.get('proposed', 0)} drafted · {nc} worth a glance")
            log_work("inbox", summary, "ok", res)
        except Exception as e:
            log_work("inbox", "inbox failed", "error", {"error": str(e)})

    async def _run_meeting(self):
        try:
            from verticals import meeting_intel
        except Exception:
            return
        try:
            res = await meeting_intel.run_once(self.broker)
            log_work("meeting", res.get("summary", "meeting scan"), "ok", res)
        except Exception as e:
            log_work("meeting", "meeting failed", "error", {"error": str(e)})

    async def run_report(self):
        from verticals.reporter import build_report
        text = build_report(window_hours=int(os.getenv("SENTINEL_REPORT_WINDOW", "24")))
        await self.router.broadcast(text)
        # advance the report anchor so the next report windows from HERE (gap-5: stops
        # the same Done item repeating across up to 8 consecutive 3h reports).
        try:
            from conductor import kv
            from datetime import datetime, timezone
            r = kv.get("reporter", {}) or {}
            r["last_at"] = datetime.now(timezone.utc).isoformat()
            kv.put("reporter", r)
        except Exception:
            pass
