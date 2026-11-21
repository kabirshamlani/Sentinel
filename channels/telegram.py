"""Telegram channel — primary approval + report surface.

Raw Bot API over httpx (no heavy dependency). Sends messages with inline
Approve/Reject buttons and long-polls getUpdates for callback_query. Token +
chat id come from the macOS keychain (SENTINEL_TELEGRAM_TOKEN /
SENTINEL_TELEGRAM_CHAT_ID), falling back to env vars.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

import httpx

logger = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"


def _keychain(name: str) -> str:
    import os
    v = os.getenv(name, "")
    if v:
        return v
    try:
        return subprocess.run(["security", "find-generic-password", "-s", name, "-w"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        return ""


class TelegramChannel:
    name = "telegram"

    def __init__(self, on_callback=None):
        self.token = _keychain("SENTINEL_TELEGRAM_TOKEN")
        self.chat_id = _keychain("SENTINEL_TELEGRAM_CHAT_ID")
        self._on_callback = on_callback     # (action_id, decision) -> None
        self._offset = 0
        self._poll_task: asyncio.Task | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _is_owner(self, chat_id: str) -> bool:
        """True only for the owner's configured chat. If chat_id somehow isn't set,
        fail CLOSED (drop) rather than open — the chat surface is privileged."""
        return bool(self.chat_id) and str(chat_id) == str(self.chat_id)

    async def _api(self, method: str, **params) -> dict:
        if not self.token:
            return {"ok": False, "error": "no token"}
        # Network faults (DNS blip 'nodename nor servname', timeouts, resets)
        # must surface as a normal {ok:False} result, not a raised exception —
        # a raise here would bubble up through send()/broadcast and (on getUpdates)
        # tight-loop the poller. Return the error so callers degrade gracefully.
        try:
            async with httpx.AsyncClient(timeout=35) as c:
                r = await c.post(API.format(token=self.token, method=method), json=params)
                return r.json()
        except Exception as e:
            return {"ok": False, "error": f"network: {type(e).__name__}: {e}"}

    async def send(self, text: str) -> str | None:
        """Send markdown-ish agent text as clean Telegram HTML.

        Markdown parse_mode broke on backticks/parens in titles; HTML with our
        own converter is robust. If HTML still fails (edge case), fall back to
        stripped plain text so the message is never silently dropped."""
        from channels.formatting import to_telegram_html, to_plain, split_message
        # Telegram rejects an empty message ('Bad Request: message text is empty').
        # An errored/empty agent turn must not reach the API as a blank send —
        # skip empties (the caller logs the underlying failure separately).
        if not (text or "").strip():
            logger.warning("telegram send skipped: empty message text")
            return None
        last_id = None
        for chunk in split_message(text or ""):
            if not chunk.strip():
                continue
            r = await self._api("sendMessage", chat_id=self.chat_id,
                                text=to_telegram_html(chunk), parse_mode="HTML",
                                disable_web_page_preview=True)
            if not r.get("ok"):
                r = await self._api("sendMessage", chat_id=self.chat_id,
                                    text=to_plain(chunk), disable_web_page_preview=True)
            if not r.get("ok"):
                logger.warning("telegram send failed: %s", r.get("description") or r.get("error"))
            else:
                last_id = str(r.get("result", {}).get("message_id", ""))
        return last_id

    async def send_approval(self, action: dict) -> str | None:
        from channels.formatting import to_telegram_html
        aid = action["id"]
        crit = "🔴 CRITICAL" if action.get("criticality") == "critical" else "🟡 routine (30-min window)"
        text = (f"⚡ **Approval needed** — {crit}\n\n"
                f"{action.get('type')} · project: {action.get('project') or '—'}\n"
                f"{action.get('description','')}\n\n"
                f"Why: {action.get('reasoning','')[:300]}")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{aid}"},
            {"text": "❌ Reject", "callback_data": f"reject:{aid}"},
        ]]}
        r = await self._api("sendMessage", chat_id=self.chat_id,
                            text=to_telegram_html(text), parse_mode="HTML",
                            reply_markup=keyboard)
        if not r.get("ok"):  # fallback plain so approvals are NEVER dropped
            from channels.formatting import to_plain
            r = await self._api("sendMessage", chat_id=self.chat_id,
                                text=to_plain(text), reply_markup=keyboard)
        if not r.get("ok"):
            logger.warning("telegram approval send failed: %s", r.get("description"))
        return str(r.get("result", {}).get("message_id", "")) if r.get("ok") else None

    async def edit_resolved(self, msg_id: str, text: str):
        try:
            await self._api("editMessageText", chat_id=self.chat_id,
                            message_id=int(msg_id), text=text)
        except Exception:
            pass

    async def edit_message(self, msg_id: str, md: str) -> bool:
        """Edit a message with HTML-rendered markdown (for turning the '🧠 …' ack bubble
        INTO the final answer, so there's no stranded progress bubble). Returns success."""
        from channels.formatting import render
        try:
            r = await self._api("editMessageText", chat_id=self.chat_id,
                                 message_id=int(msg_id), text=render(md, "telegram"),
                                 parse_mode="HTML")
            return bool(r.get("ok"))
        except Exception:
            return False

    async def start_polling(self):
        """Long-poll for button taps and free-text commands; dispatch to on_callback."""
        if not self.configured:
            logger.warning("Telegram not configured — polling disabled")
            return
        self._poll_task = asyncio.current_task()
        while True:
            try:
                r = await self._api("getUpdates", offset=self._offset, timeout=30)
                for upd in r.get("result", []):
                    self._offset = upd["update_id"] + 1
                    cq = upd.get("callback_query")
                    if cq and self._on_callback:
                        # SECURITY: only the owner's own chat may drive the bot. The
                        # chat surface includes approvals + `allow <project>` +
                        # ACP spawns; an unauthenticated sender must NOT reach it.
                        cq_chat = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                        if self._is_owner(cq_chat):
                            data = cq.get("data", "")
                            if ":" in data:
                                decision, aid = data.split(":", 1)
                                await self._api("answerCallbackQuery", callback_query_id=cq["id"],
                                                text="Got it ✅")
                                self._on_callback(aid, decision, "telegram")
                        else:
                            await self._api("answerCallbackQuery", callback_query_id=cq["id"],
                                            text="Not authorized.")
                            logger.warning("dropped callback from non-owner chat %s", cq_chat)
                    msg = upd.get("message", {})
                    txt = msg.get("text", "")
                    m_chat = str(msg.get("chat", {}).get("id", ""))
                    # Photos/files arrive with no `text` — download them and hand the
                    # agent a local path plus the caption, instead of dropping them.
                    if not txt and (msg.get("photo") or msg.get("document")):
                        if self._is_owner(m_chat):
                            path = await self._download_attachment(msg)
                            if path:
                                caption = msg.get("caption", "")
                                txt = (f"{caption}\n" if caption else "") + \
                                    f"[I sent you an image/file over Telegram — saved at {path}. Open it with Read and respond to it.]"
                        else:
                            logger.warning("dropped media from non-owner chat %s", m_chat)
                    # Forward free-text ONLY from the owner's chat id.
                    if txt and self._on_callback:
                        if self._is_owner(m_chat):
                            self._on_callback(None, "command", "telegram", txt)
                        else:
                            logger.warning("dropped message from non-owner chat %s", m_chat)
            except Exception as e:
                logger.warning("Telegram poll error: %s", e)
                await asyncio.sleep(5)

    async def _download_attachment(self, msg: dict) -> str | None:
        """Download a photo (largest size) or document from a message to
        ~/.sentinel/images/; return the local path or None."""
        try:
            if msg.get("photo"):
                sizes = msg["photo"]
                file_id = sizes[-1].get("file_id", "")   # largest rendition last
                ext = ".jpg"                             # Telegram photos are JPEG
            else:
                doc = msg.get("document", {})
                file_id = doc.get("file_id", "")
                name = doc.get("file_name", "file.bin")
                ext = os.path.splitext(name)[1] or ".bin"
            if not file_id:
                return None
            r = await self._api("getFile", file_id=file_id)
            fp = r.get("result", {}).get("file_path", "")
            if not fp:
                logger.warning("telegram getFile failed: %s", r.get("description"))
                return None
            out_dir = os.path.expanduser("~/.sentinel/images")
            os.makedirs(out_dir, exist_ok=True)
            local = os.path.join(out_dir, f"tg-{msg.get('message_id', 'x')}{ext}")
            url = f"https://api.telegram.org/file/bot{self.token}/{fp}"
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.get(url)
                resp.raise_for_status()
                with open(local, "wb") as fh:
                    fh.write(resp.content)
            logger.info("telegram attachment saved: %s (%d bytes)", local, len(resp.content))
            return local
        except Exception as e:
            logger.warning("telegram attachment download failed: %s", e)
            return None

    async def send_document(self, path: str, caption: str = "") -> bool:
        """Send a file to the user via Telegram (for 'send me that file')."""
        import os
        if not self.token or not os.path.isfile(path):
            return False
        try:
            with open(path, "rb") as fh:
                files = {"document": (os.path.basename(path), fh)}
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption[:1000]
                async with httpx.AsyncClient(timeout=120) as c:
                    r = await c.post(API.format(token=self.token, method="sendDocument"),
                                     data=data, files=files)
                    return r.json().get("ok", False)
        except Exception as e:
            logger.warning("sendDocument failed: %s", e)
            return False

    async def typing(self):
        """Show a 'typing…' indicator while the agent thinks."""
        try:
            await self._api("sendChatAction", chat_id=self.chat_id, action="typing")
        except Exception:
            pass

    async def send_photo(self, photo: str, caption: str = "") -> bool:
        """Send an image — `photo` is a URL or a local file path."""
        import os
        try:
            if photo.startswith("http"):
                r = await self._api("sendPhoto", chat_id=self.chat_id, photo=photo,
                                    caption=caption[:1000])
                return r.get("ok", False)
            if not os.path.isfile(photo):
                return False
            with open(photo, "rb") as fh:
                async with httpx.AsyncClient(timeout=120) as c:
                    r = await c.post(API.format(token=self.token, method="sendPhoto"),
                                     data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                     files={"photo": (os.path.basename(photo), fh)})
                    return r.json().get("ok", False)
        except Exception as e:
            logger.warning("sendPhoto failed: %s", e)
            return False

    async def send_media_group(self, items: list[dict]) -> bool:
        """Send up to 10 images as an album. items: [{"url": ..., "caption": ...}]."""
        import json as _json
        media = [{"type": "photo", "media": it["url"], "caption": (it.get("caption") or "")[:900]}
                 for it in items[:10] if it.get("url")]
        if not media:
            return False
        try:
            r = await self._api("sendMediaGroup", chat_id=self.chat_id, media=media)
            return r.get("ok", False)
        except Exception as e:
            logger.warning("sendMediaGroup failed: %s", e)
            return False

    async def set_commands(self):
        """Register the '/' command menu so Telegram shows autocompletion."""
        cmds = [
            {"command": "newchat", "description": "Start a fresh conversation (clear context)"},
            {"command": "status", "description": "What I've done + what's pending"},
            {"command": "tasks", "description": "Pending approvals & running work"},
            {"command": "brief", "description": "What did I miss (email/chat/calendar)"},
            {"command": "report", "description": "Full 24h work report"},
            {"command": "memory", "description": "What I know about you"},
            {"command": "models", "description": "Model performance leaderboard"},
            {"command": "autonomy", "description": "What I can do without asking (earned)"},
            {"command": "army", "description": "The full agent roster across runtimes"},
            {"command": "health", "description": "Agent health — how the army is doing"},
            {"command": "insights", "description": "Work insights I've collected"},
            {"command": "hermes", "description": "Hermes memory/skill helper status"},
            {"command": "upgrades", "description": "Check for tool/runtime upgrades"},
            {"command": "mute", "description": "Pause proactive pings (e.g. /mute 2h)"},
            {"command": "unmute", "description": "Resume proactive pings"},
            {"command": "help", "description": "Everything you can say to me"},
        ]
        try:
            await self._api("setMyCommands", commands=cmds)
        except Exception as e:
            logger.debug("setMyCommands failed: %s", e)
