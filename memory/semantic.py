"""Semantic facts — now a THIN FACADE over the unified gateway.

Historically this owned its own JSONL store; that created the "memory scattered
everywhere" mess. It now delegates to memory.gateway (the single SQLite source
of truth). The public API is unchanged so every caller keeps working:
  add_fact, all_facts, touch, retire, prompt_block, stats.

Categories map 1:1 to gateway kinds (correction/preference/profile/project/
world/fact). Corrections stay sacred (gateway never decays them).
"""

from __future__ import annotations

from memory import gateway

_CATS = {"correction", "preference", "profile", "project", "world", "fact"}


def add_fact(fact: str, category: str = "world", source: str = "chat",
             confidence: float = 0.8) -> dict:
    kind = category if category in _CATS else "fact"
    r = gateway.remember(fact, kind=kind, source=source, confidence=confidence)
    return _as_fact(r)


def all_facts(category: str | None = None) -> list[dict]:
    if category:
        return [_as_fact(r) for r in gateway.all_of(category)]
    out = []
    for c in _CATS:
        out += [_as_fact(r) for r in gateway.all_of(c)]
    return out


def touch(ids: list[str]) -> None:
    gateway.touch(ids)


def retire(fact_id: str) -> bool:
    return gateway.archive(fact_id)


def prompt_block(context_hint: str = "", budget_chars: int = 1800) -> str:
    return gateway.prompt_block(context_hint, budget_chars)


def stats() -> dict:
    g = gateway.stats()
    by_cat = {k: v for k, v in g["by_kind"].items() if k in _CATS}
    return {"active": sum(by_cat.values()), "by_category": by_cat, "archived": g["archived"]}


def _as_fact(r: dict) -> dict:
    if not r:
        return {}
    return {"id": r.get("id"), "fact": r.get("text"), "category": r.get("kind"),
            "confidence": r.get("confidence"), "source": r.get("source"),
            "created": r.get("created"), "last_used": r.get("last_used"),
            "uses": r.get("uses"), "status": r.get("status", "active")}
