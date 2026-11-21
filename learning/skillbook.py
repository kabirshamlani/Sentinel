"""Skillbook -- persistent learned strategies that agents read on boot and update after tasks.

Inspired by:
- ACE (kayba-ai/agentic-context-engine): Skillbook of learned strategies with Recursive Reflector
- Persistent Agent Framework (T33R0): auto-promoted directives from mistake patterns
- VIGIL: guarded prompt updates that modify adaptive sections while preserving core identity

Storage: SKILLBOOK_PATH (see config.paths)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config.paths import SKILLBOOK_PATH

logger = logging.getLogger("skillbook")

PROMOTION_THRESHOLD = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    """Load the skillbook from disk."""
    if SKILLBOOK_PATH.exists():
        try:
            return json.loads(SKILLBOOK_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load skillbook: %s", e)
    return {"rules": [], "strategies": []}


def _save(data: dict):
    """Save the skillbook to disk."""
    SKILLBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILLBOOK_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def get_all_rules() -> list[dict]:
    """Return all rules."""
    return _load().get("rules", [])


def get_rules_for_agent(agent_name: str) -> list[dict]:
    """Return active rules that apply to a specific agent."""
    data = _load()
    return [
        r for r in data.get("rules", [])
        if r.get("active", True)
        and (not r.get("agent_scope") or agent_name in r["agent_scope"])
    ]


def get_all_strategies() -> list[dict]:
    """Return all strategies."""
    return _load().get("strategies", [])


def has_rule_for_pattern(pattern_name: str) -> bool:
    """Check if a rule already exists for a pattern."""
    data = _load()
    return any(
        r.get("pattern_name") == pattern_name
        for r in data.get("rules", [])
    )


def promote_pattern(
    pattern_name: str,
    rule_text: str,
    error_type: str = "",
    occurrences: int = 0,
    agent_scope: list[str] | None = None,
) -> dict:
    """Create a new rule from a recurring error pattern.

    Rules earned through repeated failure carry higher priority
    than static instructions.
    """
    data = _load()

    if has_rule_for_pattern(pattern_name):
        for r in data["rules"]:
            if r["pattern_name"] == pattern_name:
                r["occurrences"] = max(r.get("occurrences", 0), occurrences)
                r["rule"] = rule_text
                r["updated"] = _now()
                _save(data)
                logger.info("Updated existing rule for pattern: %s", pattern_name)
                return r
        return {}

    rule = {
        "id": f"rule-{uuid.uuid4().hex[:8]}",
        "created": _now(),
        "source": "auto-promoted",
        "pattern_name": pattern_name,
        "occurrences": occurrences,
        "error_type": error_type,
        "rule": rule_text,
        "priority": "high",
        "agent_scope": agent_scope or [],
        "active": True,
        "last_applied": None,
        "effectiveness": None,
    }

    data["rules"].append(rule)
    _save(data)
    _mirror_to_gateway(rule_text, "high", agent_scope)
    logger.info(
        "Auto-promoted pattern '%s' to rule (occurrences=%d)",
        pattern_name, occurrences,
    )
    return rule


def add_manual_rule(
    rule_text: str,
    agent_scope: list[str] | None = None,
    priority: str = "medium",
) -> dict:
    """Add a manually created rule."""
    data = _load()
    data.setdefault("rules", [])
    data.setdefault("strategies", [])
    rule = {
        "id": f"rule-{uuid.uuid4().hex[:8]}",
        "created": _now(),
        "source": "manual",
        "pattern_name": "",
        "occurrences": 0,
        "error_type": "",
        "rule": rule_text,
        "priority": priority,
        "agent_scope": agent_scope or [],
        "active": True,
        "last_applied": None,
        "effectiveness": None,
    }
    data["rules"].append(rule)
    _save(data)
    _mirror_to_gateway(rule_text, priority, agent_scope)
    return rule


def _mirror_to_gateway(rule_text: str, priority: str = "medium",
                       agent_scope: list | None = None) -> None:
    """Dual-write every rule into the unified store so the ONE-store promise holds
    (skillbook.json stays the injection source; the gateway stays canonical)."""
    try:
        from memory import gateway
        gateway.remember(rule_text, kind="skill", source="skillbook",
                         confidence=0.75,
                         meta={"priority": priority, "agent_scope": agent_scope or []})
    except Exception:
        pass


def record_strategy(
    task_type: str,
    strategy: str,
    session_id: str = "",
    agent: str = "",
) -> dict:
    """Record a successful strategy for a task type."""
    data = _load()

    for s in data.get("strategies", []):
        if s.get("task_type") == task_type and s.get("strategy") == strategy:
            s["success_count"] = s.get("success_count", 0) + 1
            s["last_used"] = _now()
            _save(data)
            return s

    entry = {
        "id": f"strat-{uuid.uuid4().hex[:8]}",
        "task_type": task_type,
        "strategy": strategy,
        "learned_from": session_id,
        "agent": agent,
        "success_count": 1,
        "fail_count": 0,
        "created": _now(),
        "last_used": _now(),
    }
    data.setdefault("strategies", []).append(entry)
    _save(data)
    logger.info("Recorded strategy for task_type=%s", task_type)
    return entry


def record_strategy_failure(task_type: str, strategy: str):
    """Mark a strategy as having failed."""
    data = _load()
    for s in data.get("strategies", []):
        if s.get("task_type") == task_type and s.get("strategy") == strategy:
            s["fail_count"] = s.get("fail_count", 0) + 1
            _save(data)
            return
    logger.debug("No strategy found to mark as failed: %s / %s", task_type, strategy)


def update_effectiveness(rule_id: str, outcome: str):
    """Update effectiveness of a rule after it's been applied.

    outcome: "effective", "partial", "ineffective"
    """
    data = _load()
    for r in data.get("rules", []):
        if r.get("id") == rule_id:
            r["effectiveness"] = outcome
            r["last_applied"] = _now()
            if outcome == "ineffective":
                r["active"] = False
                logger.info("Deactivated ineffective rule: %s", rule_id)
            _save(data)
            return
    logger.warning("Rule not found for effectiveness update: %s", rule_id)


def get_strategies_for_task(task_description: str) -> list[dict]:
    """Find relevant strategies by keyword matching against task type."""
    data = _load()
    keywords = set(task_description.lower().split()) - {
        "the", "a", "an", "is", "to", "of", "and", "or", "in", "my", "me",
    }

    results = []
    for s in data.get("strategies", []):
        task_type_words = set(s.get("task_type", "").lower().split("-"))
        overlap = keywords & task_type_words
        if overlap:
            results.append(s)

    return sorted(results, key=lambda x: x.get("success_count", 0), reverse=True)[:3]


def build_prompt_injection(agent_name: str) -> str:
    """Build a prompt block with all active rules for an agent.

    This is injected at the start of every agent session so the agent
    reads its own 'lessons learned' before starting work.
    """
    rules = get_rules_for_agent(agent_name)
    if not rules:
        return ""

    lines = [
        "## Learned Rules (auto-generated from past mistakes -- follow these strictly)",
        "",
    ]

    high = [r for r in rules if r.get("priority") == "high"]
    medium = [r for r in rules if r.get("priority") != "high"]

    for r in high:
        lines.append(f"**[HIGH PRIORITY]** {r['rule']}")
    for r in medium:
        lines.append(f"- {r['rule']}")

    if high or medium:
        lines.append("")
        lines.append(
            "These rules were earned through repeated failures. "
            "They carry MORE weight than static instructions."
        )

    return "\n".join(lines)


def check_and_promote(error_journal_module) -> list[dict]:
    """Check error journal for patterns above threshold and auto-promote.

    Returns list of newly promoted rules.
    """
    patterns = error_journal_module.get_patterns_above_threshold(PROMOTION_THRESHOLD)
    promoted = []

    for p in patterns:
        pn = p["pattern_name"]
        if has_rule_for_pattern(pn):
            continue

        fix = p.get("latest_fix", "")
        if not fix:
            continue

        rule_text = (
            f"Avoid the '{pn}' error pattern. "
            f"When doing similar tasks: {fix}"
        )

        rule = promote_pattern(
            pattern_name=pn,
            rule_text=rule_text,
            error_type=p.get("error_type", ""),
            occurrences=p["count"],
        )
        promoted.append(rule)
        logger.info(
            "Auto-promoted pattern '%s' (%d occurrences) to skillbook rule",
            pn, p["count"],
        )

    return promoted
