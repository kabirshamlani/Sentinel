"""One-time migration: fold the legacy scattered stores into the unified gateway.

Idempotent — dedupe in gateway.remember means re-running is safe. After this,
the unified DB is the single source of truth; the old files remain on disk as a
backup but are no longer the canonical store (facades read/write the gateway).
"""

from __future__ import annotations

import json
import logging

from config.paths import MEMORY_ROOT, SKILLBOOK_PATH, ERROR_JOURNAL_PATH
from memory import gateway

logger = logging.getLogger("mem-migrate")

_MARKER = MEMORY_ROOT / ".unified_migrated"


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run(force: bool = False) -> dict:
    if _MARKER.exists() and not force:
        return {"status": "already_migrated"}
    counts = {"facts": 0, "lessons": 0, "skills": 0, "errors": 0}

    # semantic_facts.jsonl -> fact/correction/preference/profile/project/world
    for f in _read_jsonl(MEMORY_ROOT / "semantic_facts.jsonl"):
        if f.get("status", "active") != "active":
            continue
        cat = f.get("category", "world")
        kind = cat if cat in gateway.KINDS else "fact"
        gateway.remember(f.get("fact", ""), kind=kind, source=f.get("source", "legacy"),
                         confidence=f.get("confidence", 0.8),
                         meta={"migrated_from": "semantic"})
        counts["facts"] += 1

    # reasoning_bank.jsonl -> lesson (failures keep their guardrail text)
    for it in _read_jsonl(MEMORY_ROOT / "reasoning_bank.jsonl"):
        body = it.get("lesson") or it.get("strategy") or it.get("title", "")
        if not body:
            continue
        gateway.remember(f"{it.get('title','')}: {body}" if it.get("title") else body,
                         kind="lesson", source=it.get("agent", "legacy"),
                         confidence=0.7,
                         meta={"outcome": it.get("outcome"), "task_type": it.get("task_type"),
                               "migrated_from": "reasoning_bank"})
        counts["lessons"] += 1

    # skillbook.json rules -> skill
    if SKILLBOOK_PATH.exists():
        try:
            sb = json.loads(SKILLBOOK_PATH.read_text(encoding="utf-8"))
            for r in sb.get("rules", []):
                if r.get("active", True) and r.get("rule"):
                    gateway.remember(r["rule"], kind="skill", source="skillbook",
                                     confidence=0.75,
                                     meta={"priority": r.get("priority"),
                                           "agent_scope": r.get("agent_scope"),
                                           "migrated_from": "skillbook"})
                    counts["skills"] += 1
        except (json.JSONDecodeError, OSError):
            pass

    # error_journal.jsonl -> error (recent, as lessons-in-waiting)
    for e in _read_jsonl(ERROR_JOURNAL_PATH)[-100:]:
        txt = e.get("error_message") or e.get("error") or e.get("description", "")
        if txt:
            gateway.remember(txt, kind="error", source=e.get("agent", "legacy"),
                             confidence=0.5, meta={"migrated_from": "error_journal"})
            counts["errors"] += 1

    _MARKER.write_text(json.dumps({"counts": counts}), encoding="utf-8")
    logger.info("unified-memory migration: %s", counts)
    return {"status": "migrated", **counts}
