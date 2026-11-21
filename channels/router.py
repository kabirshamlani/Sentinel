"""Channel router — deliver approvals/reports to BOTH Telegram and WhatsApp.

Fans a proposed action out to every configured channel simultaneously, tracks
delivery message ids per channel, and routes any inbound approve/reject decision
(from whichever channel responds first) into the broker via broker.resolve().
Telegram is the reliable backbone; OpenClaw channels are best-effort — if none
are configured, Telegram alone still works.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("channel-router")


class ChannelRouter:
    def __init__(self, broker, telegram=None, openclaw_channels=None):
        self._broker = broker
        self._on_correction = None
        self._chat = None            # optional ChatHandler for free-text conversation
        self._channels = []
        if telegram is not None:
            telegram._on_callback = self._on_inbound
            self._channels.append(telegram)
        for ch in (openclaw_channels or []):
            ch._on_message = self._on_inbound
            self._channels.append(ch)

    @property
    def configured_names(self) -> list[str]:
        return [c.name for c in self._channels if getattr(c, "configured", False)]

    @property
    def openclaw_channels(self) -> list:
        """OpenClaw-backed channels (those that can run an inbound receive loop)."""
        return [c for c in self._channels if hasattr(c, "run_receive_loop")]

    async def deliver_approval(self, action: dict) -> dict:
        """Send the approval prompt to all channels; return {channel: msg_id}."""
        msg_ids: dict[str, str] = {}
        results = await asyncio.gather(
            *[self._safe_send_approval(c, action) for c in self._channels],
            return_exceptions=True,
        )
        for c, r in zip(self._channels, results):
            if isinstance(r, Exception) or not r:
                continue
            msg_ids[c.name] = r
        if not msg_ids:
            logger.warning("Approval for %s reached NO channel", action.get("id"))
        return msg_ids

    async def broadcast(self, text: str):
        """Send a plain status/report message to all channels."""
        await asyncio.gather(*[self._safe_send(c, text) for c in self._channels],
                             return_exceptions=True)

    async def notify_resolved(self, text: str, action: dict | None):
        await self.broadcast(text)

    def set_correction_handler(self, fn):
        """fn(text, channel) -> optionally returns a dict describing a
        compensating action for the caller to broker.propose()."""
        self._on_correction = fn

    def set_chat_handler(self, handler):
        """A ChatHandler whose .handle(text, channel) coroutine turns free-text
        into a response/action (conversational interface)."""
        self._chat = handler

    def _on_inbound(self, action_id, decision, channel, raw_text: str = ""):
        """Called by a channel when a human responds. First response wins;
        broker.resolve() ignores late duplicates."""
        if decision == "command":
            # 1) corrections ("you made a mistake") -> RCA
            if self._on_correction:
                try:
                    from learning.rca import detect_correction
                    if detect_correction(raw_text):
                        self._on_correction(raw_text, channel)
                        return
                except Exception as e:
                    logger.debug("correction check skipped: %s", e)
            # 2) explicit approve/reject/allow slash-style commands
            first = raw_text.strip().lstrip("/").split()[:1]
            if first and first[0].lower() in ("approve", "reject", "allow"):
                self._handle_command(raw_text, channel)
                return
            # 3) everything else -> conversational chat handler (if wired)
            if self._chat is not None:
                asyncio.create_task(self._chat.handle(raw_text, channel))
                return
            self._handle_command(raw_text, channel)
            return
        if action_id and decision in ("approve", "reject"):
            resolved = self._broker.resolve(action_id, decision, channel)
            logger.info("Inbound %s:%s via %s -> resolved=%s", decision, action_id, channel, resolved)

    def _handle_command(self, text: str, channel: str):
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0].lstrip("/").lower()
        if cmd in ("approve", "reject") and len(parts) >= 2:
            aid = parts[1] if parts[1].startswith("action-") else f"action-{parts[1]}"
            self._broker.resolve(aid, cmd, channel)
        elif cmd == "allow" and len(parts) >= 2:
            from approval import broker as _b
            _b.allow_all_for_project(parts[1])
            logger.info("Project %s fully pre-approved via command", parts[1])

    async def _safe_send_approval(self, ch, action):
        try:
            return await ch.send_approval(action)
        except Exception as e:
            logger.warning("send_approval failed on %s: %s", ch.name, e)
            return None

    async def _safe_send(self, ch, text):
        try:
            return await ch.send(text)
        except Exception as e:
            logger.warning("send failed on %s: %s", ch.name, e)
            return None
