"""MemoryKeeper -- the 'memory agent' that curates vault content."""

from __future__ import annotations

import re

from memory.vault import (
    get_vault_root,
    read_agent_memory,
    read_preferences,
    read_profile,
    save_handoff,
    write_agent_memory,
    write_profile,
)
from memory.context_budget import ContextBudget


_LEARNING_MARKERS = ("learned:", "note:", "important:", "remember:", "preference:")
_DECISION_MARKERS = ("decided", "chose", "will use", "going with")


def update_profile(observations: list[str]) -> None:
    """Appends observations to profile, deduplicates."""
    current = read_profile()
    existing_lines = set(current.strip().splitlines())
    new_lines = []
    for obs in observations:
        obs = obs.strip()
        if obs and obs not in existing_lines:
            new_lines.append(obs)
            existing_lines.add(obs)
    if new_lines:
        updated = current.rstrip() + "\n" + "\n".join(new_lines) + "\n"
        write_profile(updated)


def compact_memory(agent: str, max_lines: int = 200) -> None:
    """Truncate agent MEMORY.md to max lines, keeping most recent."""
    content = read_agent_memory(agent)
    if not content:
        return
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return
    compacted = lines[-max_lines:]
    write_agent_memory(agent, "\n".join(compacted) + "\n")


def extract_learnings(conversation: str) -> list[str]:
    """Use simple heuristics to extract key learnings from conversation text."""
    learnings = []
    for line in conversation.splitlines():
        line_lower = line.strip().lower()
        for marker in _LEARNING_MARKERS:
            if marker in line_lower:
                learnings.append(line.strip())
                break
    return learnings


def extract_decisions(conversation: str) -> list[dict]:
    """Extract lines that look like decisions."""
    decisions = []
    for line in conversation.splitlines():
        line_lower = line.strip().lower()
        for marker in _DECISION_MARKERS:
            if marker in line_lower:
                decisions.append({
                    "text": line.strip(),
                    "marker": marker,
                })
                break
    return decisions


def build_context_bundle(task: str, agent: str) -> str:
    """Assembles context from vault using ContextBudget."""
    budget = ContextBudget()

    global_ctx = read_profile() + "\n\n" + read_preferences()
    agent_ctx = read_agent_memory(agent)

    root = get_vault_root()
    session_ctx = ""
    handoffs_dir = root / "sessions" / "handoffs"
    if handoffs_dir.exists():
        handoff_files = sorted(handoffs_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if handoff_files:
            session_ctx = handoff_files[0].read_text(encoding="utf-8")

    task_ctx = f"Current task: {task}"

    return budget.assemble_context(global_ctx, agent_ctx, session_ctx, task_ctx)


def write_session_handoff(
    session_id: str,
    summary: str,
    key_decisions: list[str],
    pending_tasks: list[str],
) -> None:
    """Writes structured handoff markdown."""
    decisions_md = "\n".join(f"- {d}" for d in key_decisions) if key_decisions else "- None"
    pending_md = "\n".join(f"- [ ] {t}" for t in pending_tasks) if pending_tasks else "- None"

    content = (
        f"# Session Handoff: {session_id}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Key Decisions\n{decisions_md}\n\n"
        f"## Pending Tasks\n{pending_md}\n"
    )
    save_handoff(session_id, content)


def restore_session(session_id: str) -> str | None:
    """Reads handoff and formats for injection."""
    from memory.vault import get_handoff
    content = get_handoff(session_id)
    if not content:
        return None
    return f"[Previous Session Context]\n\n{content}"
