"""Memory Vault -- multi-layer persistent memory with JSONL + markdown storage.

Runtime data lives at MEMORY_ROOT (see config.paths).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config.paths import MEMORY_ROOT


def get_vault_root() -> Path:
    """Returns MEMORY_ROOT, creates if missing."""
    root = MEMORY_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_md(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    if limit is not None:
        return entries[-limit:]
    return entries


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- Global profile & preferences ---

def read_profile() -> str:
    """Reads global/profile.md."""
    return _read_md(get_vault_root() / "global" / "profile.md")


def write_profile(content: str) -> None:
    """Writes global/profile.md."""
    _write_md(get_vault_root() / "global" / "profile.md", content)


def read_preferences() -> str:
    """Reads global/preferences.md."""
    return _read_md(get_vault_root() / "global" / "preferences.md")


def write_preferences(content: str) -> None:
    """Writes global/preferences.md."""
    _write_md(get_vault_root() / "global" / "preferences.md", content)


# --- Agent-specific memory ---

def read_agent_memory(agent: str) -> str:
    """Reads agents/{agent}/MEMORY.md."""
    return _read_md(get_vault_root() / "agents" / agent / "MEMORY.md")


def write_agent_memory(agent: str, content: str) -> None:
    """Writes agents/{agent}/MEMORY.md."""
    _write_md(get_vault_root() / "agents" / agent / "MEMORY.md", content)


def read_agent_history(agent: str, limit: int = 50) -> list[dict]:
    """Reads agents/{agent}/history.jsonl."""
    path = get_vault_root() / "agents" / agent / "history.jsonl"
    return _read_jsonl(path, limit=limit)


def log_task(agent: str, task: str, result: str, score: float | None = None) -> None:
    """Appends to agent's history.jsonl."""
    path = get_vault_root() / "agents" / agent / "history.jsonl"
    entry = {
        "timestamp": _now(),
        "agent": agent,
        "task": task,
        "result": result,
        "score": score,
    }
    _append_jsonl(path, entry)


def read_decisions(agent: str) -> list[dict]:
    """Reads agents/{agent}/decisions.jsonl."""
    path = get_vault_root() / "agents" / agent / "decisions.jsonl"
    return _read_jsonl(path)


def log_decision(
    agent: str,
    decision: str,
    reasoning: str,
    alternatives: list[str] | None = None,
) -> None:
    """Appends to decisions.jsonl."""
    path = get_vault_root() / "agents" / agent / "decisions.jsonl"
    entry = {
        "timestamp": _now(),
        "agent": agent,
        "decision": decision,
        "reasoning": reasoning,
        "alternatives": alternatives or [],
    }
    _append_jsonl(path, entry)


def search_tasks(
    query: str, agent: str | None = None, limit: int = 10
) -> list[dict]:
    """Simple keyword search across task history."""
    keywords = query.lower().split()
    root = get_vault_root()

    if agent:
        agents = [agent]
    else:
        agents_dir = root / "agents"
        if not agents_dir.exists():
            return []
        agents = [d.name for d in agents_dir.iterdir() if d.is_dir()]

    results = []
    for a in agents:
        history = read_agent_history(a, limit=500)
        for entry in history:
            text = f"{entry.get('task', '')} {entry.get('result', '')}".lower()
            if any(kw in text for kw in keywords):
                results.append(entry)

    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results[:limit]


# --- Sessions ---

def get_session(session_id: str) -> dict | None:
    """Reads sessions/{session_id}.json."""
    path = get_vault_root() / "sessions" / f"{session_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_session(session_id: str, data: dict) -> None:
    """Writes session JSON."""
    path = get_vault_root() / "sessions" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_handoff(session_id: str) -> str | None:
    """Reads sessions/handoffs/{session_id}.md."""
    path = get_vault_root() / "sessions" / "handoffs" / f"{session_id}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_handoff(session_id: str, content: str) -> None:
    """Writes handoff markdown."""
    path = get_vault_root() / "sessions" / "handoffs" / f"{session_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
