"""Tiny JSON key-value state under STATE_DIR — mute flags, cursors, timestamps.

One file per key keeps writes atomic-enough and diffable. Used by the daemon
(heartbeat schedule), chat handler (/mute), and gchat bridge (poll cursors).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from config.paths import STATE_DIR


def get(name: str, default=None):
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return default


def put(name: str, value) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def muted() -> bool:
    m = get("mute")
    if not m or not m.get("until"):
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(m["until"])
    except ValueError:
        return False


def mute_for(hours: float) -> str:
    from datetime import timedelta
    until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    put("mute", {"until": until, "set_at": now_iso()})
    return until


def unmute() -> None:
    put("mute", {})
