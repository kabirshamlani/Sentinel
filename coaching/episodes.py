"""Episode capture + collection — the coach's raw material.

An *episode* is one agent run: the clean (pre-injection) input, the final
output, and the outcome signals. Today's logs record outlines only
(task_prompt[:500] polluted by the Learned-Rules prefix, result_length instead
of result text), so the coach cannot judge quality from them. Two answers:

  1. record_episode() — a tap called from brain/claude_runner.run_claude at the
     result event. Every future run of every agent becomes a faithful episode
     in ~/.sentinel/coaching/episodes.jsonl (scrubbed, capped). Best-effort:
     it must NEVER break an agent run.
  2. collect_episodes() — merges the tap stream with the legacy outline
     streams (interaction_log, autonomous_work_log, action_queue,
     conversation, error_journal) so the ~16 days of history before the tap
     existed are still reviewable, with honest `fidelity` markers.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.paths import SENTINEL_HOME

logger = logging.getLogger("coach.episodes")

COACH_DIR = SENTINEL_HOME / "coaching"
EPISODES_PATH = COACH_DIR / "episodes.jsonl"

_CAP = 8000          # max chars stored per prompt/result field
_INJECTION_SEP = "\n\n---\n\n"   # claude_runner joins injected memory + prompt with this


def _scrub(text: str) -> str:
    """Secret-redaction at the write boundary, same as every other store."""
    try:
        from memory.gateway import _scrub as gw_scrub
        return gw_scrub(text or "")
    except Exception:
        return text or ""


def record_episode(
    *,
    agent: str,
    prompt: str,
    result: str,
    tier: str = "",
    model: str = "",
    task_type: str = "",
    success: bool | None = None,
    error: str = "",
    duration_ms: int = 0,
    cost_usd: float = 0.0,
    session_id: str = "",
    cwd: str = "",
    injected_chars: int = 0,
    source: str = "run_claude",
) -> str | None:
    """Append one faithful episode. Returns the episode id, or None on failure.

    NEVER raises — a broken episode tap must not break an agent run.
    """
    try:
        COACH_DIR.mkdir(parents=True, exist_ok=True)
        eid = "ep-" + uuid.uuid4().hex[:12]
        entry = {
            "id": eid,
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent or "conductor",
            "task_type": task_type or "",
            "tier": tier,
            "model": model,
            "input": _scrub(prompt or "")[:_CAP],
            "output": _scrub(result or "")[:_CAP],
            "success": success,
            "error": _scrub(error or "")[:1000],
            "duration_ms": int(duration_ms or 0),
            "cost_usd": round(float(cost_usd or 0.0), 5),
            "session_id": session_id or "",
            "cwd": cwd or "",
            "injected_chars": int(injected_chars or 0),
            "source": source,
            "fidelity": "full",
        }
        with open(EPISODES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return eid
    except Exception as e:
        logger.debug("episode tap failed (non-fatal): %s", e)
        return None


# ─────────────────────────── collection ───────────────────────────

def _read_jsonl(path: Path, limit: int = 5000) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def strip_injection(task_prompt: str) -> str:
    """Recover the real ask from a prompt that may carry the injected
    Learned-Rules/memory prefix (claude_runner joins with '\\n\\n---\\n\\n')."""
    if not task_prompt:
        return ""
    if _INJECTION_SEP in task_prompt:
        return task_prompt.split(_INJECTION_SEP, 1)[1].strip()
    # truncated logs may contain only the prefix — flag rather than pretend
    if task_prompt.lstrip().startswith("## Learned Rules"):
        return ""
    return task_prompt.strip()


def _episode(agent: str, ts: str, inp: str, out: str, *, success=None,
             error: str = "", human_label: str = "", stream: str = "",
             fidelity: str = "outline", meta: dict | None = None) -> dict:
    # Deterministic id: the same legacy record must yield the SAME id on every
    # collect, or issue.episode_ids dangle two hours later at fix time.
    import hashlib
    key = f"{stream}|{ts}|{agent}|{(inp or '')[:200]}|{(out or '')[:200]}|{(error or '')[:100]}"
    return {
        "id": "ep-" + hashlib.sha1(key.encode()).hexdigest()[:12],
        "ts": ts,
        "agent": agent,
        # legacy source files were written unscrubbed — redact at read boundary
        # too, or secrets flow into verdicts/issues/the GitHub mirror.
        "input": _scrub(inp or "")[:_CAP],
        "output": _scrub(out or "")[:_CAP],
        "success": success,
        "error": _scrub(error or "")[:1000],
        "human_label": human_label,       # approve | reject | correction | ""
        "source": stream,
        "fidelity": fidelity,             # full | outline | output-only
        "meta": meta or {},
    }


def collect_episodes(since_hours: float = 24, agents: list[str] | None = None) -> list[dict]:
    """Merge all streams into normalized episodes, newest last.

    Each collector is independently guarded — one corrupt stream never sinks
    the review cycle.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    episodes: list[dict] = []

    def _in_window(ts_str: str) -> bool:
        ts = _parse_ts(ts_str)
        return ts is not None and ts >= cutoff

    # 1) the faithful tap stream (post-instrumentation). Coach-origin runs
    # (fix replays, repro attempts, coach-spawned code-bot) are EXCLUDED from
    # grading — the coach must never grade its own activity as production, and
    # before-replays would re-inject the very failures being fixed.
    try:
        for e in _read_jsonl(EPISODES_PATH):
            if e.get("source") == "coach-run":
                continue
            if _in_window(e.get("ts", "")):
                e.setdefault("human_label", "")
                e.setdefault("meta", {})
                episodes.append(e)
    except Exception as e:
        logger.warning("collector episodes.jsonl failed: %s", e)

    # 2) interaction_log — outlines (strip the injected prefix; result text absent)
    try:
        for e in _read_jsonl(SENTINEL_HOME / "interaction_log.jsonl"):
            if not _in_window(e.get("timestamp", "")):
                continue
            episodes.append(_episode(
                e.get("agent", "conductor"), e.get("timestamp", ""),
                strip_injection(e.get("task_prompt", "")),
                "",  # result text was never recorded — that's a coach finding, not a fact to fake
                success=e.get("success"),
                error=e.get("error_message", ""),
                stream="interaction_log",
                meta={"result_length": e.get("result_length"),
                      "session_id": e.get("session_id", ""),
                      "raw_prompt_prefixed": str(e.get("task_prompt", "")).lstrip().startswith("## Learned Rules"),
                      "error_type": e.get("error_type", "")},
            ))
    except Exception as e:
        logger.warning("collector interaction_log failed: %s", e)

    # 3) autonomous_work_log — cycle outcomes; meeting prep_notes carry full output
    try:
        for e in _read_jsonl(SENTINEL_HOME / "autonomous_work_log.jsonl"):
            if not _in_window(e.get("ts", "")):
                continue
            detail = e.get("detail") or {}
            out_text = ""
            if isinstance(detail, dict):
                out_text = str(detail.get("prep_notes") or detail.get("summary") or "")
            episodes.append(_episode(
                e.get("kind", "conductor"), e.get("ts", ""),
                e.get("summary", ""), out_text,
                success=(e.get("status") not in ("error", "failed", "fix_failed")),
                error=str((detail or {}).get("error", ""))[:500] if isinstance(detail, dict) else "",
                stream="work_log",
                fidelity="output-only" if out_text else "outline",
                meta={"status": e.get("status", ""), "detail_keys": sorted(detail.keys()) if isinstance(detail, dict) else []},
            ))
    except Exception as e:
        logger.warning("collector work_log failed: %s", e)

    # 4) action_queue — drafts/PRs with FULL text + human resolution as free labels
    try:
        qpath = SENTINEL_HOME / "action_queue.json"
        if qpath.exists():
            data = json.loads(qpath.read_text(encoding="utf-8"))
            for a in data.get("actions", []):
                if not _in_window(a.get("proposed_at", "")):
                    continue
                details = a.get("details") or {}
                body = str(details.get("body") or details.get("message") or
                           details.get("text") or details.get("content") or "")
                resolved = str(a.get("resolved_by") or "")
                label = ""
                if a.get("status") == "rejected":
                    label = "reject"
                elif resolved.startswith("user:"):
                    label = "approve"
                episodes.append(_episode(
                    a.get("source", "conductor"), a.get("proposed_at", ""),
                    a.get("description", ""), body,
                    success=(a.get("status") not in ("failed",)),
                    human_label=label, stream="action_queue",
                    fidelity="full" if body else "outline",
                    meta={"action_type": a.get("type", ""), "status": a.get("status", ""),
                          "criticality": a.get("criticality", ""), "action_id": a.get("id", "")},
                ))
    except Exception as e:
        logger.warning("collector action_queue failed: %s", e)

    # 5) chat conversation — full turns (clipped at write time by conversation.py).
    # Turns carry no per-turn timestamp; fall back to the file's mtime so recent
    # conversation stays reviewable, marked ts_estimated.
    try:
        for ch in ("telegram", "gchat"):
            cpath = SENTINEL_HOME / "state" / f"conversation_{ch}.json"
            if not cpath.exists():
                continue
            mtime_iso = datetime.fromtimestamp(cpath.stat().st_mtime, timezone.utc).isoformat()
            conv = json.loads(cpath.read_text(encoding="utf-8"))
            turns = conv.get("turns", conv if isinstance(conv, list) else [])
            pending_user = None
            for t in turns:
                if not isinstance(t, dict):
                    continue
                role = t.get("role", "")
                if role == "user":
                    pending_user = t
                elif role in ("assistant", "sentinel") and pending_user is not None:
                    ts = str(t.get("ts") or t.get("at") or pending_user.get("ts") or mtime_iso)
                    estimated = not (t.get("ts") or t.get("at") or pending_user.get("ts"))
                    if _in_window(ts):
                        episodes.append(_episode(
                            "agent-chat", ts,
                            str(pending_user.get("text") or pending_user.get("content") or ""),
                            str(t.get("text") or t.get("content") or ""),
                            success=None, stream=f"conversation_{ch}",
                            fidelity="full",
                            meta={"ts_estimated": estimated},
                        ))
                    pending_user = None
    except Exception as e:
        logger.warning("collector conversation failed: %s", e)

    # 6) error_journal — failures with signal traces
    try:
        for e in _read_jsonl(SENTINEL_HOME / "error_journal.jsonl"):
            if not _in_window(e.get("timestamp", e.get("ts", ""))):
                continue
            episodes.append(_episode(
                e.get("agent", "conductor"), e.get("timestamp", e.get("ts", "")),
                e.get("task_summary", ""), "",
                success=False,
                error=e.get("error_message", e.get("error", "")),
                stream="error_journal",
                meta={"error_type": e.get("error_type", ""),
                      "signal_trace": str(e.get("signal_trace", ""))[:300],
                      "pattern_name": e.get("pattern_name", "")},
            ))
    except Exception as e:
        logger.warning("collector error_journal failed: %s", e)

    if agents:
        want = {a.lower() for a in agents}
        episodes = [e for e in episodes if str(e.get("agent", "")).lower() in want]

    episodes.sort(key=lambda e: str(e.get("ts", "")))
    return episodes


def episode_counts(episodes: list[dict]) -> dict:
    """Per-agent counts, for the report and skip-if-nothing decisions."""
    counts: dict[str, int] = {}
    for e in episodes:
        counts[e.get("agent", "?")] = counts.get(e.get("agent", "?"), 0) + 1
    return counts


def collect_excluded_invocations(since_hours: float = 24) -> list[dict]:
    """Raw tap-stream entries in the window that `collect_episodes()` drops
    on purpose (source == "coach-run"). These are genuine invocations — the
    coach must never grade them as production, but their existence is itself
    ground truth: it separates "invoked, but unlogged to the reviewer" from
    "never invoked at all", which `unobservable_agents()` cannot otherwise
    tell apart (see coaching/review.py)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    def _in_window(ts_str: str) -> bool:
        ts = _parse_ts(ts_str)
        return ts is not None and ts >= cutoff

    excluded: list[dict] = []
    try:
        for e in _read_jsonl(EPISODES_PATH):
            if e.get("source") == "coach-run" and _in_window(e.get("ts", "")):
                excluded.append(e)
    except Exception as e:
        logger.warning("collector excluded-invocations failed: %s", e)
    return excluded
