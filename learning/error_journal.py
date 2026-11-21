"""Error Journal -- persistent JSONL ledger with signal tracing and pattern-based rule promotion.

Inspired by T33R0/persistent-agent-framework: every error is logged with
a signal trace (what specifically was misread), not just "it failed".
When the same pattern occurs 3+ times, it's auto-promoted to a Skillbook rule.

Storage: ERROR_JOURNAL_PATH (see config.paths)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from config.paths import ERROR_JOURNAL_PATH

logger = logging.getLogger("error-journal")

JOURNAL_PATH = ERROR_JOURNAL_PATH

ErrorType = Literal[
    "output_too_large",
    "chunk_exceed",
    "separator_not_found",
    "persisted_output_read_fail",
    "timeout",
    "parse_error",
    "auth_expired",
    "context_overflow",
    "rate_limited",
    "tool_not_found",
    "api_error",
    "unknown",
]

REFLEX_CATEGORIES = {
    "reasoning_error": "Agent drew wrong conclusion from correct data",
    "knowledge_gap": "Agent lacked domain knowledge needed for the task",
    "tool_failure": "Tool executed but returned error or wrong result",
    "instruction_ambiguity": "User prompt was unclear, agent guessed wrong",
    "context_loss": "Key context was lost due to summarization or truncation",
    "overconfidence": "Agent skipped verification and gave wrong answer",
}


class StuckDetector:
    """Detect progress failures -- agent repeating actions or making no progress.

    From self-healing research (Zylos 2026): progress failures are harder to detect
    than crashes but equally damaging. Uses action hash window to detect loops.
    """

    def __init__(self, window_size: int = 5):
        self._action_hashes: list[str] = []
        self._window_size = window_size
        self._stall_count = 0
        self._last_output_len = 0

    def record_action(self, action_summary: str, output_length: int = 0):
        import hashlib
        h = hashlib.md5(action_summary.encode()).hexdigest()[:8]
        self._action_hashes.append(h)
        if len(self._action_hashes) > self._window_size:
            self._action_hashes = self._action_hashes[-self._window_size:]

        if output_length == self._last_output_len and output_length > 0:
            self._stall_count += 1
        else:
            self._stall_count = 0
        self._last_output_len = output_length

    @property
    def is_looping(self) -> bool:
        if len(self._action_hashes) < 3:
            return False
        return len(set(self._action_hashes[-3:])) == 1

    @property
    def is_stalled(self) -> bool:
        return self._stall_count >= 3

    def get_recovery_hint(self) -> str:
        if self.is_looping:
            return (
                "STUCK DETECTION: You are repeating the same action. "
                "Try a COMPLETELY different approach. "
                "If a tool keeps failing, use a different tool or simplify the request."
            )
        if self.is_stalled:
            return (
                "PROGRESS STALL: Output hasn't changed in 3 iterations. "
                "Break the task into smaller steps or try an alternative strategy."
            )
        return ""

DETERMINISTIC_FIXES: dict[str, dict] = {
    "output_too_large": {
        "action": "reduce_and_summarize",
        "description": "Halve max_results, add ?summarize=true, pipe through head -c 20000",
        "prompt_suffix": (
            "RECOVERY: Previous attempt failed because output was too large. "
            "You MUST use ?summarize=true&max_results=5 on API calls. "
            "Present results as a formatted list, NOT raw JSON. "
            "Pipe long outputs through | head -c 20000."
        ),
    },
    "chunk_exceed": {
        "action": "reduce_and_summarize",
        "description": "Same as output_too_large -- CLI chunk parser failed",
        "prompt_suffix": (
            "RECOVERY: Previous attempt caused a Claude CLI chunk parsing error. "
            "The output was too large. Use ?summarize=true&max_results=5. "
            "Never return raw JSON over 15KB."
        ),
    },
    "separator_not_found": {
        "action": "reduce_and_summarize",
        "description": "RS character in output broke stream-json parser",
        "prompt_suffix": (
            "RECOVERY: Output contained special characters that broke the CLI parser. "
            "Use ?summarize=true to get plain-text output instead of JSON."
        ),
    },
    "persisted_output_read_fail": {
        "action": "chunk_read",
        "description": "Read persisted file in chunks instead of whole",
        "prompt_suffix": (
            "RECOVERY: A large output was saved to a file but reading it whole failed. "
            "Use head -c 20000 to read the first 20KB, or use a python one-liner to "
            "parse and summarize the JSON content."
        ),
    },
    "timeout": {
        "action": "retry_with_longer_timeout",
        "description": "Retry once with doubled timeout",
        "prompt_suffix": (
            "RECOVERY: Previous attempt timed out. Retry with a simpler/shorter request."
        ),
    },
    "rate_limited": {
        "action": "wait_and_retry",
        "description": "Wait 30s then retry",
        "prompt_suffix": "RECOVERY: Rate limited. Waiting before retry.",
    },
    "context_overflow": {
        "action": "compact",
        "description": "Compact conversation and retry with shorter prompt",
        "prompt_suffix": (
            "RECOVERY: Context was too long. Use a shorter, more focused prompt. "
            "Summarize context instead of including full text."
        ),
    },
    "auth_expired": {
        "action": "report",
        "description": "Report to user -- cannot auto-fix auth issues",
        "prompt_suffix": None,
    },
    "tool_not_found": {
        "action": "report",
        "description": "Check tool name and report",
        "prompt_suffix": None,
    },
    "context_too_large": {
        "action": "compact",
        "description": "Alias for context_overflow",
        "prompt_suffix": (
            "RECOVERY: Context was too long. Use a shorter, more focused prompt. "
            "Summarize context instead of including full text."
        ),
    },
    "parse_error": {
        "action": "retry_with_cleanup",
        "description": "Clean output and retry with explicit JSON formatting",
        "prompt_suffix": (
            "RECOVERY: Previous attempt returned malformed output that couldn't be parsed. "
            "Return clean, valid JSON only. Avoid special characters in string values. "
            "If reading a file, use head -c 10000 to limit output size."
        ),
    },
    "api_error": {
        "action": "wait_and_retry",
        "description": "Server error -- wait briefly then retry",
        "prompt_suffix": (
            "RECOVERY: The API returned a server error (500). This is usually transient. "
            "Try a different approach or simplify the request."
        ),
    },
    "unknown": {
        "action": "reflect_and_retry",
        "description": "Unknown error -- reflect on what went wrong and try differently",
        "prompt_suffix": (
            "RECOVERY: An unexpected error occurred. Try a completely different approach. "
            "Simplify your request, break it into smaller steps, or use a different tool."
        ),
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_path():
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)


def classify_error(error_message: str) -> ErrorType:
    """Classify an error message into a known error type."""
    msg = error_message.lower()

    if "separator is not found" in msg or "separator" in msg:
        return "separator_not_found"
    if "chunk exceed" in msg or "chunk" in msg and "limit" in msg:
        return "chunk_exceed"
    if "output too large" in msg or "persisted-output" in msg and "too large" in msg:
        return "output_too_large"
    if "persisted" in msg or "full output saved" in msg:
        return "persisted_output_read_fail"
    if "timeout" in msg or "timed out" in msg or "idle for" in msg or "stopped producing output" in msg:
        return "timeout"
    if "rate" in msg and "limit" in msg or "429" in msg:
        return "rate_limited"
    if ("context" in msg or "conversation" in msg) and ("overflow" in msg or "too long" in msg or "exceed" in msg or "bytes" in msg):
        return "context_overflow"
    if "401" in msg or "403" in msg or "forbidden" in msg or "unauthorized" in msg or "auth" in msg:
        return "auth_expired"
    if "not found" in msg or "404" in msg:
        return "tool_not_found"
    if "parse" in msg or "json" in msg and "decode" in msg:
        return "parse_error"
    if "500" in msg or "internal server" in msg:
        return "api_error"

    return "unknown"


def get_deterministic_fix(error_type: ErrorType) -> dict | None:
    """Return the deterministic fix for a known error type, or None if unknown."""
    return DETERMINISTIC_FIXES.get(error_type)


def log_error(
    agent: str,
    session_id: str,
    task_summary: str,
    error_type: ErrorType,
    error_message: str,
    signal_trace: str,
    what_should_have_happened: str,
    recovery_action: str = "",
    recovery_succeeded: bool = False,
    pattern_name: str = "",
) -> dict:
    """Log a structured error entry to the journal."""
    _ensure_path()

    entry = {
        "timestamp": _now(),
        "agent": agent,
        "session_id": session_id,
        "task_summary": task_summary[:200],
        "error_type": error_type,
        "error_message": error_message[:500],
        "signal_trace": signal_trace,
        "what_should_have_happened": what_should_have_happened,
        "recovery_action": recovery_action,
        "recovery_succeeded": recovery_succeeded,
        "pattern_name": pattern_name or f"{error_type}-{agent}",
    }

    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # dual-write into the unified store so ALL knowledge lives in one place
    # (the JSONL stays the clustering source; the gateway stays canonical).
    try:
        from memory import gateway
        gateway.remember(f"{task_summary[:80]}: {error_message[:200]}", kind="error",
                         source=agent, confidence=0.5,
                         meta={"error_type": error_type, "pattern": entry["pattern_name"],
                               "recovered": recovery_succeeded})
    except Exception:
        pass

    logger.info(
        "Error logged: type=%s pattern=%s recovered=%s",
        error_type, entry["pattern_name"], recovery_succeeded,
    )
    return entry


def get_all_entries() -> list[dict]:
    """Read all journal entries."""
    if not JOURNAL_PATH.exists():
        return []
    entries = []
    for line in JOURNAL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def get_recent_errors(agent: str | None = None, limit: int = 20) -> list[dict]:
    """Get recent errors, optionally filtered by agent."""
    entries = get_all_entries()
    if agent:
        entries = [e for e in entries if e.get("agent") == agent]
    return entries[-limit:]


def count_patterns() -> dict[str, int]:
    """Count occurrences of each pattern_name across all entries."""
    entries = get_all_entries()
    return dict(Counter(e.get("pattern_name", "unknown") for e in entries))


def get_patterns_above_threshold(threshold: int = 3) -> list[dict]:
    """Return patterns that have occurred >= threshold times, with details."""
    entries = get_all_entries()
    pattern_entries: dict[str, list[dict]] = {}
    for e in entries:
        pn = e.get("pattern_name", "")
        if pn:
            pattern_entries.setdefault(pn, []).append(e)

    results = []
    for pattern_name, occurrences in pattern_entries.items():
        if len(occurrences) >= threshold:
            latest = occurrences[-1]
            results.append({
                "pattern_name": pattern_name,
                "count": len(occurrences),
                "error_type": latest.get("error_type", "unknown"),
                "latest_signal_trace": latest.get("signal_trace", ""),
                "latest_fix": latest.get("what_should_have_happened", ""),
                "first_seen": occurrences[0].get("timestamp", ""),
                "last_seen": latest.get("timestamp", ""),
                "recovery_rate": sum(
                    1 for o in occurrences if o.get("recovery_succeeded")
                ) / len(occurrences),
            })

    return sorted(results, key=lambda x: x["count"], reverse=True)


def generate_reflection(entry: dict) -> str:
    """Distill an error entry into a reusable verbal reflection (Reflexion pattern).

    Instead of just 'what to do', captures 'why it failed and what to notice'.
    This is more useful for the agent than raw error messages.
    """
    task = entry.get("task_summary", "unknown task")
    signal = entry.get("signal_trace", "")
    fix = entry.get("what_should_have_happened", "")
    etype = entry.get("error_type", "unknown")

    return (
        f"When doing '{task[:60]}': "
        f"misread signal '{signal[:80]}'. "
        f"Correct approach: {fix[:100]}. "
        f"(error type: {etype})"
    )


def get_reflections_for_task(
    task_description: str, agent: str | None = None, limit: int = 3
) -> list[str]:
    """Retrieve relevant verbal reflections for a new task (Reflexion retrieval).

    These are higher-quality than raw warnings -- they explain WHY something failed
    and what the correct approach was.
    """
    entries = get_all_entries()
    if agent:
        entries = [e for e in entries if e.get("agent") == agent]

    recovered = [e for e in entries if e.get("recovery_succeeded")]
    if not recovered:
        recovered = entries[-10:]

    keywords = set(task_description.lower().split()) - {
        "the", "a", "an", "is", "to", "of", "and", "or", "in", "my", "me",
    }
    if not keywords:
        return []

    scored = []
    for entry in recovered:
        searchable = f"{entry.get('task_summary', '')} {entry.get('signal_trace', '')}".lower()
        match_count = sum(1 for kw in keywords if kw in searchable)
        if match_count >= 1:
            scored.append((match_count, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [generate_reflection(entry) for _, entry in scored[:limit]]


def get_warnings_for_task(task_description: str, agent: str | None = None) -> list[str]:
    """Search past errors for relevant warnings to inject before a new task.

    Uses simple keyword matching between the task description and past signal traces.
    """
    keywords = set(task_description.lower().split())
    relevant_keywords = keywords - {
        "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
        "and", "or", "in", "on", "at", "for", "with", "my", "me", "i",
        "can", "you", "do", "show", "get", "from", "please", "what",
    }

    if not relevant_keywords:
        return []

    entries = get_all_entries()
    if agent:
        entries = [e for e in entries if e.get("agent") == agent]

    warnings = []
    seen_patterns = set()

    for entry in reversed(entries):
        pattern = entry.get("pattern_name", "")
        if pattern in seen_patterns:
            continue

        searchable = (
            f"{entry.get('task_summary', '')} {entry.get('signal_trace', '')} "
            f"{entry.get('what_should_have_happened', '')}"
        ).lower()

        match_count = sum(1 for kw in relevant_keywords if kw in searchable)
        if match_count >= 2 or (match_count >= 1 and len(relevant_keywords) <= 3):
            fix = entry.get("what_should_have_happened", "")
            if fix:
                warnings.append(
                    f"WARNING from past error ({pattern}): {fix}"
                )
                seen_patterns.add(pattern)

        if len(warnings) >= 5:
            break

    return warnings
