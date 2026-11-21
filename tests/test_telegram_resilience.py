"""Regression tests: chat must never look dead on transient/empty failures.

2026-08-04: The owner saw the agent go silent for days. Root causes:
(1) a DNS blip made _api RAISE instead of returning {ok:False}, and
(2) an errored turn produced an empty message that Telegram rejects
    ('Bad Request: message text is empty') — so the reply silently vanished.
"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from channels.telegram import TelegramChannel


def _chan():
    c = TelegramChannel.__new__(TelegramChannel)
    c.token = "test-token"; c.chat_id = "123"
    c._offset = 0; c._on_callback = None; c._poll_task = None
    return c


def test_api_returns_dict_on_network_error(monkeypatch):
    c = _chan()
    class BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            raise OSError("nodename nor servname provided, or not known")
    import channels.telegram as tg
    monkeypatch.setattr(tg.httpx, "AsyncClient", BoomClient)
    r = asyncio.run(c._api("getUpdates"))
    assert isinstance(r, dict) and r["ok"] is False and "network" in r["error"]


def test_send_skips_empty_text(monkeypatch):
    c = _chan()
    calls = []
    async def fake_api(method, **params):
        calls.append(params.get("text")); return {"ok": True, "result": {"message_id": 1}}
    monkeypatch.setattr(c, "_api", fake_api)
    assert asyncio.run(c.send("")) is None
    assert asyncio.run(c.send("   \n ")) is None
    assert calls == []                       # never hit the API with empty text
    asyncio.run(c.send("real message"))
    assert calls == ["real message"] or any("real message" in (x or "") for x in calls)
