"""Blackboard — the shared state pool specialists read instead of handing off.

Research verdict (doc 11 §e): message-passing handoffs lose context (~37% of
multi-agent failures). A blackboard — one shared memory all specialists can read
— fixes it and cuts total tokens. This is a thin, bounded view over the live
work context + recent decisions/findings other agents have posted.

Writers post short entries; readers get a compact snapshot injected into their
prompt. Bounded and ephemeral (last N entries); durable knowledge belongs in
semantic memory / reasoning_bank, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from conductor import kv

MAX_ENTRIES = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post(author: str, kind: str, text: str) -> None:
    """A specialist posts a finding/decision for others to see.
    kind: finding | decision | blocker | artifact."""
    if not (text or "").strip():
        return
    board = kv.get("blackboard", []) or []
    board.append({"author": author, "kind": kind, "text": text.strip()[:300], "at": _now()})
    kv.put("blackboard", board[-MAX_ENTRIES:])


def snapshot_text(max_chars: int = 1000) -> str:
    """Compact rendering for injection into a specialist prompt."""
    board = kv.get("blackboard", []) or []
    # include the ambient work context as the always-present base layer
    work = ""
    try:
        from tools import observer
        work = observer.summary_text(max_chars=500)
    except Exception:
        pass
    lines = []
    if work:
        lines.append(work)
    if board:
        lines.append("Recent notes from other agents:")
        for e in board[-8:]:
            lines.append(f"- [{e['author']}/{e['kind']}] {e['text']}")
    return "\n".join(lines)[:max_chars]


def clear() -> None:
    kv.put("blackboard", [])
