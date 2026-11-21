"""ERL-inspired heuristic extraction and retrieval."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memory.vault import get_vault_root


@dataclass
class Heuristic:
    """A learned pattern-action pair with confidence tracking."""

    id: str
    agent: str
    pattern: str
    action: str
    confidence: float
    successes: int = 0
    failures: int = 0
    created: str = ""
    last_used: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _heuristics_path(agent: str) -> Path:
    return get_vault_root() / "agents" / agent / "heuristics.jsonl"


def get_all_heuristics(agent: str) -> list[Heuristic]:
    """Read all heuristics from JSONL."""
    path = _heuristics_path(agent)
    if not path.exists():
        return []
    heuristics = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                heuristics.append(Heuristic(**data))
    return heuristics


def save_heuristic(agent: str, heuristic: Heuristic) -> None:
    """Append heuristic to agents/{agent}/heuristics.jsonl."""
    path = _heuristics_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(heuristic), ensure_ascii=False) + "\n")


def extract_heuristics(
    task_description: str, outcome: str, success: bool
) -> list[Heuristic]:
    """Parse outcome for actionable patterns.

    Looks for lines with 'when' or 'if' as trigger patterns and
    'then', 'should', 'do' as action indicators.
    """
    heuristics = []
    lines = outcome.splitlines()

    for line in lines:
        line_lower = line.strip().lower()
        pattern = ""
        action = ""

        if "when " in line_lower or "if " in line_lower:
            parts = line.strip().split(",", 1)
            if len(parts) == 2:
                pattern = parts[0].strip()
                action = parts[1].strip()
            elif " then " in line_lower:
                parts = line_lower.split(" then ", 1)
                pattern = parts[0].strip()
                action = parts[1].strip()
            elif " should " in line_lower:
                parts = line_lower.split(" should ", 1)
                pattern = parts[0].strip()
                action = parts[1].strip()

        if pattern and action:
            heuristics.append(Heuristic(
                id=str(uuid.uuid4())[:8],
                agent="",
                pattern=pattern,
                action=action,
                confidence=0.7 if success else 0.3,
                successes=1 if success else 0,
                failures=0 if success else 1,
                created=_now(),
                last_used=_now(),
            ))

    if not heuristics and task_description:
        heuristics.append(Heuristic(
            id=str(uuid.uuid4())[:8],
            agent="",
            pattern=f"task resembles: {task_description[:100]}",
            action=f"outcome was {'successful' if success else 'unsuccessful'}",
            confidence=0.5,
            successes=1 if success else 0,
            failures=0 if success else 1,
            created=_now(),
            last_used=_now(),
        ))

    return heuristics


def retrieve_relevant(
    task_description: str, agent: str, top_k: int = 5
) -> list[Heuristic]:
    """Keyword overlap scoring for retrieval."""
    all_h = get_all_heuristics(agent)
    if not all_h:
        return []

    task_words = set(task_description.lower().split())

    scored = []
    for h in all_h:
        h_words = set(f"{h.pattern} {h.action}".lower().split())
        overlap = len(task_words & h_words)
        score = overlap * h.confidence
        if score > 0:
            scored.append((score, h))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in scored[:top_k]]


def update_confidence(agent: str, heuristic_id: str, success: bool) -> None:
    """Update success/failure counts and recalculate confidence."""
    all_h = get_all_heuristics(agent)
    path = _heuristics_path(agent)

    updated = []
    for h in all_h:
        if h.id == heuristic_id:
            if success:
                h.successes += 1
            else:
                h.failures += 1
            total = h.successes + h.failures
            h.confidence = h.successes / total if total > 0 else 0.5
            h.last_used = _now()
        updated.append(h)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for h in updated:
            f.write(json.dumps(asdict(h), ensure_ascii=False) + "\n")
