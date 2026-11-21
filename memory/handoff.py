"""Structured handoff protocol between agents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class Handoff:
    """Data structure for inter-agent handoffs."""

    from_agent: str
    to_agent: str
    task_summary: str
    findings: list[str]
    artifacts: list[dict]
    context: str
    acceptance_criteria: list[str]
    timestamp: str = ""


def create_handoff(
    from_agent: str,
    to_agent: str,
    task_summary: str,
    findings: list[str],
    artifacts: list[dict],
    context: str,
    acceptance_criteria: list[str],
) -> Handoff:
    """Create a new Handoff with current timestamp."""
    return Handoff(
        from_agent=from_agent,
        to_agent=to_agent,
        task_summary=task_summary,
        findings=findings,
        artifacts=artifacts,
        context=context,
        acceptance_criteria=acceptance_criteria,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def serialize_handoff(handoff: Handoff) -> str:
    """JSON string, truncated to <=600 tokens (~2400 chars)."""
    MAX_CHARS = 2400
    data = asdict(handoff)
    result = json.dumps(data, ensure_ascii=False)
    if len(result) <= MAX_CHARS:
        return result
    data["context"] = data["context"][:500] + "..."
    data["findings"] = data["findings"][:5]
    result = json.dumps(data, ensure_ascii=False)
    if len(result) <= MAX_CHARS:
        return result
    return result[:MAX_CHARS - 50] + '..."}'


def parse_handoff(json_str: str) -> Handoff:
    """Validate and parse JSON string into Handoff."""
    data = json.loads(json_str)
    return Handoff(
        from_agent=data.get("from_agent", ""),
        to_agent=data.get("to_agent", ""),
        task_summary=data.get("task_summary", ""),
        findings=data.get("findings", []),
        artifacts=data.get("artifacts", []),
        context=data.get("context", ""),
        acceptance_criteria=data.get("acceptance_criteria", []),
        timestamp=data.get("timestamp", ""),
    )


def compute_artifact_hash(filepath: str) -> str:
    """SHA256 of file contents."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
