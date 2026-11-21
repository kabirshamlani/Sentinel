"""ReasoningBank — now a THIN FACADE over the unified gateway.

Distills each task into a retrievable lesson; failures become preventative
guardrails. Storage is the single SQLite gateway (kind='lesson', with
meta.outcome/task_type), not a separate JSONL — fixing the scattered-memory
mess. Public API unchanged: record, record_failure, retrieve, retrieve_enriched,
stats, all_items, prune.
"""

from __future__ import annotations

from memory import gateway


def _title(objective: str) -> str:
    return (objective or "").strip()[:120]


def record(objective: str, agent: str = "", task_type: str = "general",
           outcome: str = "done", result: str = "", lesson: str = "") -> dict:
    body = lesson or (result[:200] if outcome == "done" else "") or _title(objective)
    text = f"{_title(objective)}: {body}" if _title(objective) and body != _title(objective) else _title(objective)
    conf = 0.7 if outcome == "done" else 0.6
    r = gateway.remember(text, kind="lesson", source=agent or "system", confidence=conf,
                         meta={"outcome": outcome, "task_type": task_type,
                               "objective": _title(objective)})
    return _as_item(r)


def record_failure(objective: str, agent: str, error: str, task_type: str = "general") -> dict:
    lesson = f"Previously failed: {error[:200]}. Watch for this."
    r = gateway.remember(f"{_title(objective)}: {lesson}", kind="lesson",
                         source=agent or "system", confidence=0.65,
                         meta={"outcome": "failed", "task_type": task_type,
                               "objective": _title(objective)})
    return _as_item(r)


def retrieve(objective: str, task_type: str = "", k: int = 4) -> str:
    hits = gateway.search(objective, kinds=["lesson", "skill", "error"], k=k * 2)
    if not hits:
        return ""
    # failures/guardrails first
    hits.sort(key=lambda h: (h.get("meta", {}).get("outcome") != "failed",
                             -h.get("confidence", 0)))
    lines = ["Lessons from past similar tasks (respect the guardrails):"]
    for h in hits[:k]:
        failed = h.get("meta", {}).get("outcome") == "failed"
        tag = "⚠️ FAILED BEFORE" if failed else "✓"
        lines.append(f"- [{tag}] {h['text']}")
    return "\n".join(lines)


async def retrieve_enriched(objective: str, task_type: str = "", k: int = 4) -> str:
    """Gateway lessons + Hermes FTS5 recall when Hermes is enabled (isolated).
    Ours is authoritative; Hermes is pure hints and silently absent otherwise."""
    base = retrieve(objective, task_type=task_type, k=k)
    try:
        from brain import hermes_adapter
        extra = await hermes_adapter.recall(objective, k=3)
        if extra:
            base += "\n(Also recalled — hints only):\n" + "\n".join(f"- {e}" for e in extra)
    except Exception:
        pass
    return base


def stats() -> dict:
    lessons = gateway.all_of("lesson")
    failed = sum(1 for l in lessons if l.get("meta", {}).get("outcome") == "failed")
    return {"total": len(lessons), "failed": failed, "done": len(lessons) - failed}


def all_items() -> list[dict]:
    return [_as_item(r) for r in gateway.all_of("lesson")]


def prune(keep: int = 400) -> int:
    """Consolidation is owned by consolidate.semantic_maintenance() ->
    gateway.consolidate() (runs once nightly over the whole store). This just
    reports the current lesson count so nightly() has a number, without a second
    redundant consolidate pass."""
    return len(gateway.all_of("lesson"))


def _as_item(r: dict) -> dict:
    if not r:
        return {}
    m = r.get("meta", {})
    return {"id": r.get("id"), "title": m.get("objective", r.get("text", "")[:120]),
            "task_type": m.get("task_type", "general"), "lesson": r.get("text"),
            "outcome": m.get("outcome", "done"), "agent": r.get("source"),
            "uses": r.get("uses"), "last_used": r.get("last_used")}
