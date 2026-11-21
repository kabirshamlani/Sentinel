"""OpenClaw bridge — the multi-channel delivery + reach layer (hybrid role).

Architecture decision (agent-army-research/12): OpenClaw is the ONLY real
always-on multi-channel gateway, so it owns channels/delivery/reach. It does NOT
own orchestration or the safety broker — those stay in our deterministic Python
Conductor, and the security floor stays in Claude Code's kernel sandbox
(OpenClaw's ACP runs host-side, unsandboxed, and its releases are churny). So we
use OpenClaw strictly as a MOUTH (send) and, once channels are linked, EARS
(inbound), never as the brain.

Delivery uses the real CLI:  openclaw message send --channel <ch> --target <t>
  -m <text> [--buttons JSON] [--media path] [--json]
Supported channels: whatsapp, telegram, imessage, googlechat, discord, signal, …

Graceful degradation is the whole point: if OpenClaw isn't installed, the
gateway is down, or a channel isn't linked yet (WhatsApp needs a QR scan the owner
does), delivery returns False and Sentinel's own Telegram path carries
everything. Nothing on the safety-critical path depends on OpenClaw being up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

logger = logging.getLogger("openclaw")

# channels OpenClaw can deliver to (superset; only linked ones actually send)
KNOWN_CHANNELS = {"whatsapp", "telegram", "imessage", "googlechat", "discord",
                  "signal", "slack", "matrix", "msteams", "line"}


def available() -> bool:
    return shutil.which("openclaw") is not None


async def _run(*args, timeout: int = 40) -> tuple[int, str, str]:
    if not available():
        return 127, "", "openclaw not installed"
    try:
        proc = await asyncio.create_subprocess_exec(
            "openclaw", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")
    except asyncio.TimeoutError:
        return 124, "", f"timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


async def linked_channels() -> dict:
    """Which channels are actually linked & ready to deliver."""
    rc, out, _ = await _run("channels", "list", "--json", timeout=20)
    ready = {}
    if rc == 0:
        try:
            d = json.loads(out)
            for ch, accounts in (d.get("chat") or {}).items():
                ready[ch] = accounts
        except json.JSONDecodeError:
            pass
    return ready


async def send(channel: str, target: str, text: str,
               buttons: list | None = None, media: str | None = None) -> bool:
    """Deliver a message via OpenClaw. Returns True only on confirmed send."""
    if channel not in KNOWN_CHANNELS:
        return False
    args = ["message", "send", "--channel", channel, "--target", str(target),
            "-m", text[:4000], "--json"]
    if buttons:
        args += ["--buttons", json.dumps(buttons)]
    if media:
        args += ["--media", media]
    rc, out, err = await _run(*args)
    if rc != 0:
        # "Channel is unavailable" / "not linked" is expected until the owner links it
        logger.info("openclaw send(%s) not delivered: %s", channel,
                    (err or out).strip()[:140])
        return False
    return True


class OpenClawChannel:
    """A channels.router-compatible channel that delivers via OpenClaw.

    Registered alongside the native Telegram channel; the router fans approvals/
    messages to both, first-response-wins. Silent no-op until its channel links."""

    def __init__(self, channel: str, target: str, on_message=None):
        self.name = channel                # "whatsapp" | "googlechat" | "imessage" | ...
        self.target = target
        self._on_message = on_message

    @property
    def configured(self) -> bool:
        # "configured" if OpenClaw exists and we have a target; readiness (linked)
        # is checked lazily at send time so an unlinked channel just no-ops.
        return available() and bool(self.target)

    async def send(self, text: str) -> str | None:
        from channels.formatting import render
        ok = await send(self.name, self.target, render(text, self.name))
        return "sent" if ok else None

    async def send_approval(self, action: dict) -> str | None:
        from channels.formatting import render
        aid = action["id"]
        crit = "🔴 CRITICAL" if action.get("criticality") == "critical" else "🟡 routine (30-min)"
        body = (f"⚡ Approval needed — {crit}\n{action.get('type')} · "
                f"{action.get('description','')}\n\nWhy: {action.get('reasoning','')[:200]}")
        short = aid.replace("action-", "")
        # native buttons on channels that support them (Telegram/Discord/etc.);
        # channels without buttons (WhatsApp) get the numbered-reply fallback text.
        buttons = [[{"text": "✅ Approve", "data": f"approve:{aid}"},
                    {"text": "❌ Reject", "data": f"reject:{aid}"}]]
        if self.name in ("whatsapp", "imessage", "signal"):
            body += f"\n\nReply: 1=approve / 2=reject  (action {short})"
            buttons = None
        ok = await send(self.name, self.target, render(body, self.name), buttons=buttons)
        return short if ok else None

    async def edit_resolved(self, msg_id: str, text: str):
        await send(self.name, self.target, text)   # channels vary on edit; send a note

    async def send_document(self, path: str, caption: str = "") -> bool:
        return await send(self.name, self.target, caption or "file", media=path)

    # ── inbound: poll OpenClaw for new messages FROM the owner and dispatch them ──
    # (Wave 3: fixes the dead `_on_message` — before this, OpenClaw was outbound-only
    #  and the owner could receive mirrored approvals but not COMMAND Sentinel from
    #  WhatsApp/iMessage. Now his messages feed the same router -> broker -> owner-gate.)

    def _sender_is_owner(self, msg: dict) -> bool:
        """Fail-closed owner gate. The owner's own number/handle only. Configured via
        SENTINEL_OWNER_HANDLES (comma-sep E.164/handles/usernames); if unset we treat
        the DM `target` itself as the trusted peer (a 1:1 thread to the owner's own number)."""
        import os
        sender = str(msg.get("sender") or msg.get("from") or msg.get("author") or "").strip().lower()
        if not sender:
            return False
        allow = {h.strip().lower() for h in os.getenv("SENTINEL_OWNER_HANDLES", "").split(",") if h.strip()}
        if not allow and self.target:
            allow = {str(self.target).strip().lower()}
        # match on suffix (E.164 with/without +) or exact handle
        return any(sender.endswith(a.lstrip("+")) or sender == a for a in allow)

    async def run_receive_loop(self, poll_seconds: int = 15) -> None:
        """Poll `openclaw message read` for new inbound messages and dispatch the owner's
        to the router via _on_message. No-ops safely until the channel is linked."""
        import asyncio
        if not (available() and self._on_message and self.target):
            return
        last_id = ""
        logger.info("openclaw receive loop starting for channel=%s target=%s", self.name, self.target)
        while True:
            try:
                args = ["message", "read", "--channel", self.name, "--target", str(self.target),
                        "--limit", "10", "--json"]
                if last_id:
                    args += ["--after", last_id]
                rc, out, err = await _run(*args, timeout=30)
                if rc == 0 and out.strip():
                    msgs = _parse_messages(out)
                    for m in msgs:
                        mid = str(m.get("id") or m.get("message_id") or "")
                        if mid:
                            last_id = mid
                        text = (m.get("text") or m.get("body") or "").strip()
                        if not text:
                            continue
                        if not self._sender_is_owner(m):
                            logger.info("openclaw inbound from non-owner on %s — ignored", self.name)
                            continue
                        # dispatch as a conversational command (router owner-gates + routes to chat)
                        self._on_message(None, "command", self.name, text)
            except Exception as e:
                logger.debug("openclaw receive poll error (%s): %s", self.name, e)
            await asyncio.sleep(poll_seconds)


def _parse_messages(out: str) -> list[dict]:
    """Normalize `openclaw message read --json` output to a list of message dicts."""
    import json
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(d, list):
        return [m for m in d if isinstance(m, dict)]
    for k in ("messages", "result", "data", "items"):
        v = d.get(k) if isinstance(d, dict) else None
        if isinstance(v, list):
            return [m for m in v if isinstance(m, dict)]
    return []
