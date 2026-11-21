"""Agent factory — build (never write) a `.claude/agents/<slug>.md` subagent
spec and return a broker-ready ``create_agent`` proposal.

Creating a subagent expands Sentinel's autonomous surface, so it is a CRITICAL,
IRREVERSIBLE action that must be approved by a human. This module only *builds*
the proposed Markdown content and the propose(...) payload; it does NOT write
the file. The write happens (repo-local, under `.claude/agents/`) only after the
ApprovalBroker resolves the proposal.

Pure stdlib only (re, json, logging, inspect).
"""

from __future__ import annotations

import inspect
import json
import logging
import re

logger = logging.getLogger("agent-factory")

# Where an approved agent file would live (repo-local; NEVER global ~/.claude).
AGENTS_DIR_REL = ".claude/agents"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "agent"


def build_agent_markdown(name: str, role: str, tools: list[str]) -> str:
    """Return the `.claude/agents/<slug>.md` file content: YAML frontmatter
    (name, description, tools) followed by the role/system prompt body."""
    slug = slugify(name)
    tool_list = ", ".join(t.strip() for t in (tools or []) if t and t.strip()) or "Read, Grep, Glob"
    # keep the description single-line/escaped so the frontmatter stays valid
    desc = " ".join((role or "").split())
    desc_yaml = json.dumps(desc)  # safe-quotes any special chars
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {desc_yaml}\n"
        f"tools: {tool_list}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{role}\n\n"
        "## Operating rules\n"
        "- You are approval-gated: propose irreversible or critical actions via\n"
        "  the ApprovalBroker; never self-install skills, MCP servers, or agents.\n"
        "- Discovery is read-only until a human approves.\n"
    )


def create_agent_spec(name: str, role: str, tools: list[str]) -> dict:
    """Build a broker-ready ``create_agent`` proposal payload. Does NOT write the
    file — returns the proposed content and the exact propose(...) kwargs.

    The returned dict has:
      - ``action_type`` / ``description`` / ``details`` / ``reasoning`` /
        ``project`` / ``criticality_hint`` / ``reversibility``  (propose kwargs)
      - ``details.agent_path`` : repo-local target path (.claude/agents/<slug>.md)
      - ``details.content``     : the Markdown that WOULD be written on approval
    """
    slug = slugify(name)
    content = build_agent_markdown(name, role, tools)
    agent_path = f"{AGENTS_DIR_REL}/{slug}.md"
    reasoning = (f"Create subagent '{slug}' (role: {(' '.join((role or '').split()))[:120]}) "
                 f"with tools [{', '.join(tools or [])}]. Repo-local target {agent_path}; "
                 f"NEVER global ~/.claude. Critical + irreversible — human approval required.")
    return {
        "action_type": "create_agent",
        "description": f"Create agent {slug}",
        "details": {
            "name": name,
            "slug": slug,
            "role": role,
            "tools": list(tools or []),
            "agent_path": agent_path,
            "content": content,
            "install_target": ".claude/agents/ (repo-local; NEVER global ~/.claude)",
        },
        "reasoning": reasoning,
        "project": "sentinel",
        "criticality_hint": "critical",
        "reversibility": "irreversible",
    }


def _is_awaitable(obj) -> bool:
    return inspect.iscoroutine(obj) or inspect.isawaitable(obj)


def _run_propose(broker, **kwargs):
    result = broker.propose(**kwargs)
    if not _is_awaitable(result):
        return result
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    return result


def propose_agent(broker, name: str, role: str, tools: list[str]) -> dict:
    """Build the spec and submit it to the broker as an approval-gated proposal.
    Never writes the agent file directly."""
    spec = create_agent_spec(name, role, tools)
    logger.info("Proposing approval-gated agent creation: %s -> %s",
                spec["details"]["slug"], spec["details"]["agent_path"])
    proposal = _run_propose(broker, **{k: spec[k] for k in (
        "action_type", "description", "details", "reasoning",
        "project", "criticality_hint", "reversibility")})
    return {"spec": spec, "decision": "proposed", "proposal": proposal}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(json.dumps(create_agent_spec(
        "Log Triage Bot", "Read logs and summarize error spikes read-only.",
        ["Read", "Grep", "Glob"]), indent=2))
