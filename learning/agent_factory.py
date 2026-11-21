"""Agent factory — Sentinel creates and repairs its own agents (gated).

"I want these agents to self create a new agent, repair any agent" — the
factory can draft a new .claude/agents/<slug>.md (a real Claude Code subagent)
or a repaired prompt for a failing one. Both are SELF-MODIFICATION and
therefore always CRITICAL approvals with the full file content in the
proposal; the executor only writes inside the repo's .claude/ dir.

Repair trigger: error_journal shows >=3 failures attributed to one agent.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from config.paths import REPO_ROOT

logger = logging.getLogger("agent-factory")

AGENTS_DIR = REPO_ROOT / ".claude" / "agents"

_TEMPLATE = """---
name: {slug}
description: {description}
tools: {tools}
---

{system_prompt}

## Non-negotiables
- Never push, merge, or send anything external — propose through the broker.
- Verify claims structurally (run the check) before reporting success.
- If blocked, report ONE line and stop; don't thrash.
"""


def list_agents() -> list[dict]:
    out = []
    if AGENTS_DIR.exists():
        for f in sorted(AGENTS_DIR.glob("*.md")):
            desc = ""
            try:
                m = re.search(r"^description:\s*(.+)$", f.read_text(), re.M)
                desc = m.group(1).strip() if m else ""
            except OSError:
                pass
            out.append({"slug": f.stem, "path": str(f), "description": desc[:120]})
    return out


def draft_agent(slug: str, description: str, system_prompt: str,
                tools: str = "Read, Grep, Glob, Bash") -> dict:
    """Build the proposal payload for creating a new agent (critical)."""
    slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())[:40]
    content = _TEMPLATE.format(slug=slug, description=description.strip()[:200],
                               tools=tools, system_prompt=system_prompt.strip())
    return {
        "action_type": "create_agent",
        "description": f"Create new agent '{slug}': {description[:80]}",
        "details": {"path": str(AGENTS_DIR / f"{slug}.md"), "content": content},
        "reasoning": "New capability needed; agent definitions are self-modification "
                     "and always require your approval.",
        "project": "sentinel-self", "tier": "A", "reversibility": "reversible",
        "criticality_hint": "critical",
    }


def failing_agents(threshold: int = 3) -> list[str]:
    """Agents with >= threshold recent errors in the journal."""
    try:
        from learning.error_journal import get_recent_errors
        errors = get_recent_errors(limit=50)
    except Exception:
        return []
    counts: dict[str, int] = {}
    for e in errors:
        agent = e.get("agent") or e.get("source") or ""
        if agent:
            counts[agent] = counts.get(agent, 0) + 1
    return [a for a, n in counts.items() if n >= threshold]


async def draft_repair(slug: str) -> dict | None:
    """Ask a read-tier agent to rewrite a failing agent's prompt using its
    error history; returns a critical proposal or None."""
    path = AGENTS_DIR / f"{slug}.md"
    if not path.exists():
        return None
    current = path.read_text()
    try:
        from learning.error_journal import get_recent_errors
        errors = get_recent_errors(agent=slug, limit=8)
    except Exception:
        errors = []
    err_text = "\n".join(
        f"- {e.get('error_message', e.get('error', e.get('description','')))[:150]}"
        for e in errors) or "(no detail)"
    from brain import claude_runner
    prompt = (
        f"This Claude Code agent definition keeps failing. Rewrite it to prevent the "
        f"failures — tighten instructions, add explicit guardrails for the observed "
        f"errors, keep the same frontmatter fields.\n\nCurrent definition:\n{current}\n\n"
        f"Recent failures:\n{err_text}\n\nReturn ONLY the complete new file content."
    )
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             allowed_tools=[], model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    if not out.strip().startswith("---"):
        return None
    return {
        "action_type": "self_modify",
        "description": f"Repair failing agent '{slug}' ({len(errors)} recent errors)",
        "details": {"path": str(path), "content": out.strip()},
        "reasoning": f"error_journal shows repeated failures for {slug}; proposed a "
                     "hardened prompt. Diff applies only after your approval.",
        "project": "sentinel-self", "tier": "A", "reversibility": "reversible",
        "criticality_hint": "critical",
    }
