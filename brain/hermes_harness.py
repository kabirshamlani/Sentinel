"""Hermes-as-main-harness driver.

ARCHITECTURE DECISION (the owner, 2026-08-14, overriding the v0.9 shelving):
Hermes is the main harness — the always-on decision core. Division of labor:

  HERMES (profile `sentinel`)  judges the timeline, opens/dismisses missions,
                               proposes drafts, remembers decisions. Toolsets
                               locked to memory/skills/todo + the sentinel MCP
                               bridge; NO terminal/file/exec — its hands are
                               the bridge's structured tools only.
  DAEMON (conductor)           supervisor + plumbing: perception pollers,
                               credential custody, the approval broker and
                               Telegram poller, and the mission EXECUTOR
                               (Claude Code in worktrees). It fires Hermes's
                               own cron scheduler (`hermes cron tick`) and
                               falls back to the internal judge only when
                               Hermes is broken — resilience, not distrust.
  CLAUDE CODE                  the sandboxed hands that do the actual work,
                               spawned per task by the executor.

Scheduling lives IN Hermes (`hermes -p sentinel cron`): jobs are visible and
editable with `hermes -p sentinel cron list/edit`, with durable run history
(`cron runs`). The daemon only supplies wall power (a tick every few minutes
with credentials injected per-spawn — no secrets ever land in Hermes config).
"""

from __future__ import annotations

import logging
import re

from brain import hermes_adapter as ha

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

logger = logging.getLogger("hermes-harness")

PROFILE = "sentinel"
TICK_TIMEOUT = 480          # a mission-scan turn = several tool calls + judgment


def available() -> bool:
    return ha.available()


CHAT_TOOLSETS = "memory,skills,todo,sentinel"      # 'sentinel' = the MCP bridge
CHAT_TIMEOUT = 420


async def chat_turn(request: str, transcript: str,
                    self_knowledge: str = "") -> str | None:
    """One orchestrator chat turn: Hermes reasons over the conversation and
    acts through the sentinel bridge (spawn_claude subagents, missions,
    proposals). Returns the reply for the owner, or None → caller falls back to
    the direct Claude loop. This is the 'Hermes orchestrates chat too' path
    (the owner, 2026-08-14)."""
    if not available():
        return None
    from prompts import load
    prompt = load("chat.orchestrator",
                  transcript=(transcript or "(new conversation)")[-6000:],
                  request=request[:4000])
    if self_knowledge:
        prompt += ("\n\nSENTINEL SELF-KNOWLEDGE (authoritative — answer from "
                   "THIS, never from any model's self-image):\n" + self_knowledge[:2500])
    try:
        rc, out = await ha._run("-p", PROFILE, "chat", "-Q",
                                "-t", CHAT_TOOLSETS, "-q", prompt,
                                timeout=CHAT_TIMEOUT)
    except Exception as e:
        logger.warning("hermes chat_turn crashed: %s", e)
        return None
    text = _ANSI_RE.sub("", out or "")
    lines = [ln for ln in text.splitlines()
             if not ln.strip().startswith(("⚠️", "Warning:"))]
    text = "\n".join(lines).strip()
    if rc != 0 or not text or "HTTP 401" in text or "HTTP 403" in text:
        logger.warning("hermes chat_turn unusable (rc=%s): %s", rc, text[:150])
        return None
    return text[:3900]


SWEEP_TOOLSETS = "memory,skills,todo,sentinel"


async def tick() -> bool:
    """Run the harness mission-sweep doctrine as a one-shot Hermes chat turn.

    WHY NOT `hermes cron tick`: hermes executes cron JOBS in a clean context
    that does NOT inherit the tick process's env — proven live 2026-08-14
    (a tick with full credential injection still produced a 401 job run).
    One-shot chat turns DO inherit the per-spawn credentials and were verified
    working repeatedly. The `mission-scan` cron job stays registered so the
    sweep also fires under a future `hermes gateway install` (that mode needs
    a profile .env with the key — the owner's call, it puts a secret on disk).
    Returns True when the sweep ran cleanly — False routes the caller to the
    internal fallback judge."""
    if not available():
        return False
    from prompts import load
    doctrine = load("mission.harness_scan")
    try:
        env = ha._child_env()   # diagnostic, no secrets: injection intact?
        logger.info("sweep env: gateway_key=%s custom_base=%s",
                    bool(env.get("GATEWAY_API_KEY")), bool(env.get("CUSTOM_BASE_URL")))
        rc, out = await ha._run("-p", PROFILE, "chat", "-Q",
                                "-t", SWEEP_TOOLSETS, "-q", doctrine,
                                timeout=TICK_TIMEOUT)
    except Exception as e:
        logger.warning("hermes sweep crashed: %s", e)
        return False
    tail = (out or "").strip()[-400:]
    if rc != 0 or "HTTP 401" in tail or "HTTP 403" in tail:
        logger.warning("hermes sweep rc=%s: %s", rc, tail[-200:])
        return False
    logger.info("hermes sweep: %s", tail[-250:] or "(quiet)")
    return True
