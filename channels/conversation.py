"""Conversation memory — Sentinel remembers what you were just talking about.

Fixes the "two messages in continuation have no context" gap: every inbound and
outbound chat message is persisted per channel, and the recent transcript (plus
structured context slots like "the emails I just showed you") is injected into
the reasoning prompt, so follow-ups like "reply to the second one" or "do that"
work naturally. /newchat clears it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from config.paths import STATE_DIR

MAX_TURNS = 80    # persisted cap per channel
WINDOW = 14       # turns injected into prompts
CLIP = 1200       # chars stored per message (long replies stored truncated)

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "last": -1, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Conversation:
    def __init__(self, channel: str = "telegram"):
        self.channel = channel
        self.path = STATE_DIR / f"conversation_{channel}.json"

    # ---- storage ----

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"turns": [], "slots": {}, "started_at": _now()}

    def _save(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    # ---- turns ----

    def add(self, role: str, text: str, kind: str = "message") -> None:
        """role: 'user' | 'sentinel'. kind: message | approval | heartbeat | file."""
        if not (text or "").strip():
            return
        d = self._load()
        d["turns"].append({"role": role, "text": text.strip()[:CLIP], "kind": kind, "at": _now()})
        d["turns"] = d["turns"][-MAX_TURNS:]
        self._save(d)

    def recent(self, n: int = WINDOW) -> list[dict]:
        return self._load()["turns"][-n:]

    def transcript(self, n: int = WINDOW) -> str:
        rows = []
        for t in self.recent(n):
            who = "the owner" if t["role"] == "user" else "Sentinel"
            rows.append(f"{who}: {t['text']}")
        return "\n".join(rows)

    def clear(self) -> None:
        self._save({"turns": [], "slots": {}, "started_at": _now()})

    # ---- context slots (structured "what I just showed you") ----

    def remember(self, key: str, value) -> None:
        d = self._load()
        d["slots"][key] = {"value": value, "at": _now()}
        d["slots"] = dict(list(d["slots"].items())[-12:])   # keep it bounded
        self._save(d)

    def recall(self, key: str, default=None):
        s = self._load()["slots"].get(key)
        return s["value"] if s else default

    def slots_block(self) -> str:
        """Compact rendering of context slots for prompts."""
        d = self._load()["slots"]
        if not d:
            return ""
        lines = []
        for k, v in list(d.items())[-6:]:
            val = json.dumps(v["value"], ensure_ascii=False)[:400]
            lines.append(f"- {k}: {val}")
        return "Recent context (things I showed/did):\n" + "\n".join(lines)


def ordinal_in(text: str) -> int | None:
    """Parse 'the 2nd one' / 'second email' / 'last one' -> index (1-based, -1=last)."""
    low = text.lower()
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            return idx
    m = re.search(r"\b(?:number|no\.?|#)\s*(\d{1,2})\b", low)
    return int(m.group(1)) if m else None
