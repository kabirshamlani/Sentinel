"""Learning Loop -- connects error journal, skillbook, task classifier, and feedback.

This is the self-improvement engine that makes agents smarter over time.

Implements:
1. Pre-task: Inject learned rules + warnings from past errors
2. Post-task: Extract lessons, record strategies, log errors
3. Feedback: Track user corrections and update behavior
4. Promotion: Auto-promote recurring patterns to rules

Based on research from:
- Reflexion (Shinn et al.): attempt → evaluate → reflect → retry
- Voyager (NVIDIA): skill library that grows over time
- ACE: Recursive Reflector for continuous improvement
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config.paths import SENTINEL_HOME
from learning import error_journal, skillbook

logger = logging.getLogger("learning-loop")

FEEDBACK_LOG = SENTINEL_HOME / "feedback_log.jsonl"
INTERACTION_LOG = SENTINEL_HOME / "interaction_log.jsonl"


def pre_task_injection(agent_name: str, task_prompt: str) -> str:
    """Generate a prompt injection block with all learned intelligence.

    Called BEFORE every agent invocation. Returns text to prepend to the system prompt.
    """
    sections = []

    rules_block = skillbook.build_prompt_injection(agent_name)
    if rules_block:
        sections.append(rules_block)

    reflections = error_journal.get_reflections_for_task(task_prompt, agent=agent_name)
    if reflections:
        ref_lines = ["## Reflections from Past Mistakes (Reflexion pattern)"]
        for r in reflections:
            ref_lines.append(f"- {r}")
        sections.append("\n".join(ref_lines))

    warnings = error_journal.get_warnings_for_task(task_prompt, agent=agent_name)
    if warnings:
        sections.append("## Warnings from Past Errors\n" + "\n".join(warnings))

    strategies = skillbook.get_strategies_for_task(task_prompt)
    if strategies:
        strat_lines = ["## Known Successful Strategies"]
        for s in strategies:
            win_rate = s.get("success_count", 0) / max(
                s.get("success_count", 0) + s.get("fail_count", 0), 1
            )
            strat_lines.append(
                f"- {s['strategy'][:120]} (win rate: {win_rate:.0%})"
            )
        sections.append("\n".join(strat_lines))

    if not sections:
        return ""

    return "\n\n".join(sections) + "\n"


def post_task_analysis(
    agent_name: str,
    session_id: str,
    task_prompt: str,
    result_text: str,
    success: bool,
    error_message: str = "",
    duration_ms: int = 0,
) -> dict:
    """Analyze task outcome and update learning systems.

    Called AFTER every agent invocation. Logs the interaction and triggers learning.
    """
    _ensure_path(INTERACTION_LOG)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent_name,
        "session_id": session_id,
        "task_prompt": task_prompt[:500],
        "success": success,
        "duration_ms": duration_ms,
        "result_length": len(result_text),
    }

    if not success and error_message:
        error_type = error_journal.classify_error(error_message)
        entry["error_type"] = error_type
        entry["error_message"] = error_message[:300]

        error_journal.log_error(
            agent=agent_name,
            session_id=session_id,
            task_summary=task_prompt[:200],
            error_type=error_type,
            error_message=error_message[:500],
            signal_trace=_extract_signal_trace(error_message),
            what_should_have_happened=_suggest_fix(error_type, task_prompt),
        )

        promoted = skillbook.check_and_promote(error_journal)
        if promoted:
            entry["promoted_rules"] = [r["pattern_name"] for r in promoted]
            logger.info("Auto-promoted %d rules from error patterns", len(promoted))

    if success:
        task_type = _classify_task_type(task_prompt)
        strategy = _extract_strategy(task_prompt, result_text)
        if strategy:
            skillbook.record_strategy(
                task_type=task_type,
                strategy=strategy,
                session_id=session_id,
                agent=agent_name,
            )
            entry["recorded_strategy"] = strategy

    with open(INTERACTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def record_feedback(
    session_id: str,
    task_prompt: str,
    feedback_type: str,
    feedback_detail: str = "",
    agent_name: str = "",
) -> dict:
    """Record user feedback (correction, approval, rejection).

    feedback_type: 'correction', 'approval', 'rejection', 'edit'
    """
    _ensure_path(FEEDBACK_LOG)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "agent": agent_name,
        "task_prompt": task_prompt[:300],
        "feedback_type": feedback_type,
        "feedback_detail": feedback_detail[:500],
    }

    with open(FEEDBACK_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if feedback_type == "correction" and feedback_detail:
        skillbook.add_manual_rule(
            rule_text=f"User correction: {feedback_detail}",
            agent_scope=[agent_name] if agent_name else [],
            priority="high",
        )
        logger.info("Created rule from user correction: %s", feedback_detail[:80])

    return entry


def get_learning_stats() -> dict:
    """Get stats about the learning system's growth."""
    rules = skillbook.get_all_rules()
    strategies = skillbook.get_all_strategies()
    errors = error_journal.get_all_entries()

    active_rules = [r for r in rules if r.get("active", True)]
    auto_rules = [r for r in active_rules if r.get("source") == "auto-promoted"]
    manual_rules = [r for r in active_rules if r.get("source") == "manual"]

    feedbacks = _read_jsonl(FEEDBACK_LOG)
    interactions = _read_jsonl(INTERACTION_LOG)

    successful = [i for i in interactions if i.get("success")]
    failed = [i for i in interactions if not i.get("success")]

    return {
        "total_interactions": len(interactions),
        "success_rate": round(len(successful) / max(len(interactions), 1), 2),
        "total_errors_logged": len(errors),
        "total_rules": len(active_rules),
        "auto_promoted_rules": len(auto_rules),
        "manual_rules": len(manual_rules),
        "total_strategies": len(strategies),
        "total_feedbacks": len(feedbacks),
        "correction_count": sum(1 for f in feedbacks if f.get("feedback_type") == "correction"),
        "top_error_types": _top_n(errors, "error_type", 5),
        "top_strategies": sorted(
            strategies, key=lambda s: s.get("success_count", 0), reverse=True
        )[:5],
    }


def _extract_signal_trace(error_message: str) -> str:
    """Extract key signal from error message."""
    lines = error_message.strip().split("\n")
    key_parts = []
    for line in lines[-5:]:
        line = line.strip()
        if line and len(line) < 200:
            key_parts.append(line)
    return " | ".join(key_parts) if key_parts else error_message[:200]


def _suggest_fix(error_type: str, task_prompt: str) -> str:
    """Generate a fix suggestion based on error type."""
    fix = error_journal.get_deterministic_fix(error_type)
    if fix and fix.get("description"):
        return fix["description"]
    return f"Review and retry the task: {task_prompt[:100]}"


def _classify_task_type(prompt: str) -> str:
    """Quick classification of task type for strategy recording."""
    prompt_lower = prompt.lower()
    if any(w in prompt_lower for w in ["code", "implement", "script", "function"]):
        return "coding"
    if any(w in prompt_lower for w in ["research", "search", "find out", "investigate"]):
        return "research"
    if any(w in prompt_lower for w in ["design", "architect", "system"]):
        return "architecture"
    if any(w in prompt_lower for w in ["review", "audit", "security"]):
        return "review"
    if any(w in prompt_lower for w in ["document", "report", "write up"]):
        return "documentation"
    if any(w in prompt_lower for w in ["calendar", "email", "meeting", "workspace"]):
        return "workspace"
    return "general"


def _extract_strategy(prompt: str, result: str) -> str:
    """Extract a reusable strategy description from a successful task."""
    task_type = _classify_task_type(prompt)
    clean = prompt
    for prefix in ["[MODE: autonomous]\n", "[MEMORY CONTEXT]"]:
        if prefix in clean:
            clean = clean.split("[/MEMORY CONTEXT]")[-1].strip() if "[/MEMORY CONTEXT]" in clean else clean.split(prefix)[0].strip()
    prompt_short = clean[:80].strip()

    if task_type == "coding" and ("test" in result.lower() or "pass" in result.lower()):
        return f"Write code then test it immediately. Task: {prompt_short}"
    if task_type == "research":
        return f"Use web_fetch for research, cross-reference sources. Task: {prompt_short}"
    if task_type == "workspace":
        return f"Use workspace API with ?summarize=true. Task: {prompt_short}"

    return f"Completed {task_type} task successfully. Approach: {prompt_short}"


def _ensure_path(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _top_n(entries: list[dict], key: str, n: int) -> list[tuple[str, int]]:
    from collections import Counter
    counts = Counter(e.get(key, "unknown") for e in entries)
    return counts.most_common(n)
