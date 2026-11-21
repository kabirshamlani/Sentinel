"""Google Chat bridge — trigger Sentinel from GChat, like Cursor's chat bot.

gog reality (inspected live): spaces have {resource: "spaces/XXX", name, type};
DMs have NO display name. Raw message senders are "users/<id>", but
tools.gog_workspace resolves them to real names via the People API
(people/<id>, kv-cached) and attaches sender_name on every chat read.

So the ONLY-OWNER GUARD is self-calibrating: at startup the bridge samples
recent messages across his DM spaces — the sender id that appears in ≥3
DIFFERENT DMs is necessarily the owner (he is the one common participant in all
his own DMs). That id is cached and every trigger must come from it; if
calibration hasn't converged yet, triggers are IGNORED (fail closed).

Watches (rewritten 2026-08-14 after the live "what did Riley say" test failed):
ALL spaces — DMs and group spaces — tiered by ACTIVITY. The old design polled
"the first 8 DMs in gog's arbitrary order + explicitly-watched names", and a
48h ground-truth scan showed 100% of the owner's recent chat lives in GROUP spaces
none of which were watched. Now: a startup full sweep (backfills history into
the timeline), then HOT spaces (any message ≤7 days) every cycle + a rotating
few COLD spaces per cycle, so a dormant space that wakes up is noticed within
~40 min and then promoted to hot. Trigger: message starting with `@sentinel` /
`sentinel:` / `!s`. Replies go to the same space with a 🤖 prefix (they come
from his account); group replies are mirrored to Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import re

from conductor import kv

logger = logging.getLogger("gchat-bridge")

TRIGGER = re.compile(r"^\s*(?:@?sentinel[,:]?|!s)\s+(.+)$", re.I | re.S)
POLL_SECONDS = 120
HOT_DAYS = 7                 # a space with a message in the last week is HOT
COLD_PER_CYCLE = 3           # dormant spaces probed round-robin each cycle
CALIBRATE_MIN_SPACES = 3


def _sender_key(m: dict) -> tuple[str, str]:
    """(users/<id>, display) — gog returns sender as a plain string in some
    versions and a {name, displayName} dict in others; handle both. (The old
    string-only assumption silently broke the wake-word owner check.)
    Display preference: gog's People-API-resolved `sender_name`
    ("Name <email>" — the identity-fusion key) > dict displayName > raw id."""
    snd = m.get("sender")
    if isinstance(snd, dict):
        uid = str(snd.get("name", ""))
        disp = str(m.get("sender_name") or snd.get("displayName", "") or uid)
        return uid, disp
    uid = str(snd or "")
    return uid, str(m.get("sender_name") or uid)


def _space_id(s: dict) -> str:
    rid = s.get("resource") or s.get("space_id") or ""
    if rid.startswith("spaces/"):
        return rid
    n = s.get("name") or ""
    return n if str(n).startswith("spaces/") else ""


class GChatBridge:
    def __init__(self, chat_handler, config: dict | None = None):
        self.chat = chat_handler
        self.config = config or {}
        self._spaces: list[dict] = []          # [{id, display, is_dm}]
        self._owner_id: str = kv.get("gchat_identity", {}).get("user_id", "") \
            if kv.get("gchat_identity") else ""

    # ------------------------------------------------------------- discovery

    async def _refresh_spaces(self):
        """Discover ALL spaces (DM + group). Which ones get polled per cycle
        is decided by the activity tiers in run(), not by list order."""
        from tools import gog_workspace as gw
        if not gw.available():
            return
        out = []
        try:
            for s in await gw.list_spaces(60):
                sid = _space_id(s)
                if not sid:
                    continue
                stype = str(s.get("spaceType") or s.get("type") or "").upper()
                disp = s.get("name") or ""
                is_dm = "DIRECT" in stype
                out.append({"id": sid, "display": disp or ("(DM)" if is_dm else sid),
                            "is_dm": is_dm})
        except Exception as e:
            logger.debug("space refresh failed: %s", e)
            return
        if out:
            self._spaces = out

    async def _calibrate_identity(self):
        """Find the owner's users/<id>: the sender common to many of his DM spaces."""
        if self._owner_id:
            return
        from tools import gog_workspace as gw
        seen: dict[str, set] = {}
        for space in [s for s in self._spaces if s["is_dm"]][:6]:
            try:
                msgs = await gw.read_space_messages(space["id"], 8)
            except Exception:
                continue
            for m in msgs if isinstance(msgs, list) else []:
                if not isinstance(m, dict):
                    continue
                uid, _ = _sender_key(m)
                if uid.startswith("users/"):
                    seen.setdefault(uid, set()).add(space["id"])
        best = max(seen.items(), key=lambda kv_: len(kv_[1]), default=None)
        if best and len(best[1]) >= CALIBRATE_MIN_SPACES:
            self._owner_id = best[0]
            kv.put("gchat_identity", {"user_id": best[0],
                                      "spaces_seen": len(best[1])})
            logger.info("gchat identity calibrated (%s across %d DMs)",
                        best[0][:20], len(best[1]))
        else:
            logger.info("gchat identity not yet calibrated (%d candidates) — "
                        "group triggers stay disabled", len(seen))

    # ---------------------------------------------------------------- polling

    def _sender_is_owner(self, msg: dict) -> bool:
        uid, _ = _sender_key(msg)
        return bool(self._owner_id) and uid == self._owner_id

    async def _poll_space(self, space: dict):
        from tools import gog_workspace as gw
        cursors = kv.get("gchat_cursors", {}) or {}
        last_seen = cursors.get(space["id"], "")
        try:
            msgs = await gw.read_space_messages(space["id"], 10)
        except Exception:
            return
        if not isinstance(msgs, list):
            return
        first_run = not last_seen
        newest = last_seen
        ambient: list[dict] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            ts = str(m.get("createTime") or "")
            text = (m.get("text") or "").strip()
            if ts and ts > newest:
                newest = ts
            if not text or (ts and last_seen and ts <= last_seen):
                continue
            # AMBIENT CAPTURE: every new message — not just @sentinel
            # wake-words — lands in the cross-source timeline, so a colleague's
            # "have you got the email?" ping correlates with their email and
            # escalates a mission. On first sight of a space the fetched
            # window is BACKFILLED (ingest-only) so history exists immediately;
            # triggers are skipped on first_run (never answer old wake-words).
            uid, disp_name = _sender_key(m)
            ambient.append({
                "id": str(m.get("name") or f"{space['id']}/{ts}"),
                "source": "gchat_dm" if space.get("is_dm") else "gchat_space",
                "ts": ts or kv.now_iso(),
                "sender": disp_name or uid,
                "sender_key": uid,
                "channel": space.get("display", space.get("id", "")),
                "kind": "self" if (self._owner_id and uid == self._owner_id) else "message",
                "text": text,
            })
            if first_run:
                continue
            trig = TRIGGER.match(text)
            if not trig:
                continue
            if not self._sender_is_owner(m):
                logger.info("gchat trigger from non-the owner sender in %s — ignored",
                            space["display"])
                continue
            ask = trig.group(1).strip()
            logger.info("gchat trigger in %s: %s", space["display"], ask[:60])
            asyncio.create_task(self._answer(space, ask))
        if ambient:
            try:
                from memory import timeline
                timeline.ingest(ambient)
            except Exception as e:
                logger.debug("gchat timeline ingest failed: %s", e)
        # activity map drives the hot/cold polling tiers
        if newest:
            act = kv.get("gchat_activity", {}) or {}
            if act.get(space["id"]) != newest:
                act[space["id"]] = newest
                kv.put("gchat_activity", act)
        if newest != last_seen or first_run:
            cursors[space["id"]] = newest or kv.now_iso()
            kv.put("gchat_cursors", cursors)

    async def _answer(self, space: dict, ask: str):
        from tools import gog_workspace as gw
        from channels.formatting import to_gchat

        async def reply_via(text: str):
            await gw.send_chat(space["id"], "🤖 " + to_gchat(text)[:3800])
            if not space.get("is_dm"):          # audit trail for group replies
                try:
                    await self.chat.router.broadcast(
                        f"💬 Replied in GChat · {space['display']}: {text[:150]}")
                except Exception:
                    pass

        await self.chat.handle(ask, channel="gchat", reply_via=reply_via)

    def _tiers(self) -> tuple[list[dict], list[dict]]:
        """(hot, cold): hot = message within HOT_DAYS or never probed at
        startup sweep; cold = dormant, probed round-robin."""
        from datetime import datetime, timedelta, timezone
        act = kv.get("gchat_activity", {}) or {}
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=HOT_DAYS)).isoformat().replace("+00:00", "Z")
        hot, cold = [], []
        for s in self._spaces:
            (hot if str(act.get(s["id"], "")) >= cutoff else cold).append(s)
        return hot, cold

    async def run(self):
        await self._refresh_spaces()
        await self._calibrate_identity()
        logger.info("GChat bridge: %d spaces discovered (identity=%s) — full "
                    "startup sweep, then hot/cold polling",
                    len(self._spaces), "ok" if self._owner_id else "PENDING")
        # one-time full sweep: probes every space (backfills history, seeds
        # the activity map) so nothing stays invisible until its cold turn
        try:
            for space in self._spaces:
                await self._poll_space(space)
        except Exception as e:
            logger.warning("gchat startup sweep error: %s", e)
        hot, cold = self._tiers()
        logger.info("GChat bridge live: %d hot / %d cold spaces", len(hot), len(cold))
        refresh_counter = 0
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                hot, cold = self._tiers()
                for space in hot:
                    await self._poll_space(space)
                if cold:
                    idx = int(kv.get("gchat_cold_idx", 0) or 0)
                    for i in range(min(COLD_PER_CYCLE, len(cold))):
                        await self._poll_space(cold[(idx + i) % len(cold)])
                    kv.put("gchat_cold_idx", (idx + COLD_PER_CYCLE) % max(len(cold), 1))
                refresh_counter += 1
                if refresh_counter >= 15:        # rediscover every ~30 min
                    await self._refresh_spaces()
                    if not self._owner_id:
                        await self._calibrate_identity()
                    refresh_counter = 0
            except Exception as e:
                logger.warning("gchat poll error: %s", e)
