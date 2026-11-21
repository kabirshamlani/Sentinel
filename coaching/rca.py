"""RCA — turn failing verdicts into CLASSIFIED, clustered issues.

Rules that came out of the research and are enforced here:

  * Cluster first, classify once — never patch test-by-test (oscillation).
    Failures group by (agent, check); an issue needs >= the rubric's
    min_occurrences before it exists at all (below that = transient noise,
    logged to the error journal only — skip-if-nothing).
  * One primary class per issue, from a FIXED-ORDER tree:
      transient -> spec-wrong -> missing-context -> tool-defect
      -> architecture-defect -> prompt-defect
    The decision rule for architecture-vs-prompt: if the fix could be
    circumvented by the model ignoring an instruction, it is architecture,
    never prompt (knowledge base law #1).
  * The failing agent is never asked what went wrong (self-report is poison);
    classification is evidence-keyed off WHICH deterministic check failed,
    where each check's failure mode was designed with a known cause chain.
  * Every issue carries its evidence (episode ids + check evidence) and a
    suggested fix channel, so the fixer and the human reviewer can verify the
    chain end to end.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("coach.rca")

CLASSES = ("transient", "spec-wrong", "missing-context", "tool-defect",
           "architecture-defect", "prompt-defect")

# check -> (rca_class, fix_channel, default fix target, why this class)
# fix_channel: code | prompt | rubric | instrumentation
_CLASS_RULES: dict[str, tuple[str, str, str, str]] = {
    # A literal None in a task target means the CALLER formatted a null —
    # the tool/pipeline code, independent of any prompt.
    "none_in_target": (
        "tool-defect", "code", "verticals/github_autopilot.py",
        "a null issue-number flowed through perceive/pick/format unchecked — "
        "no prompt wording can stop a caller-side null"),
    # Produced-no-commit is investigated as pipeline-first: verify the task the
    # agent was HANDED before blaming the agent (fix #None proved the caller).
    "commit_evidence": (
        "tool-defect", "code", "verticals/github_autopilot.py",
        "no-commit runs cluster with malformed inputs; verify the handed task "
        "before touching the agent prompt"),
    # Empty review counted as clean is calling-code logic (the 'lgtm'/empty
    # check), a gate the model can't opt out of — architecture.
    "review_not_empty": (
        "architecture-defect", "code", "verticals/github_autopilot.py",
        "an empty/failed review run must fail the review gate, not pass it; "
        "that decision lives in the caller, not the reviewer's prompt"),
    # Forced drafts with no decline path — the prompt lacks the SKIP hatch.
    "draft_nonempty_or_skip": (
        "prompt-defect", "prompt", "prompts/inbox/draft_gchat.md",
        "the draft prompt forces output with no 'no reply needed' escape; "
        "add SKIP + honor it in the caller"),
    # The model complaining about its input = the pipeline fed bad context.
    "no_meta_complaints": (
        "missing-context", "code", "",
        "the deliverable contains the model's complaint about truncated/missing "
        "input — fix what is retrieved/injected, not the wording"),
    # success=True on garbage output — the success LABEL definition is wrong.
    "success_label_consistent": (
        "spec-wrong", "instrumentation", "learning/learning_loop.py",
        "the success signal is defined as 'produced text', so every downstream "
        "learner trains on mislabeled outcomes; fix the label definition"),
    "sources_present": (
        "prompt-defect", "prompt", ".claude/agents/researcher.md",
        "research answers ship without sources — the contract must demand them"),
    "no_placeholder_sources": (
        "prompt-defect", "prompt", ".claude/agents/researcher.md",
        "placeholder/fabricated citations — forbid explicitly and demand "
        "honest 'no source found'"),
    "answer_not_promise": (
        "prompt-defect", "prompt", "prompts/chat/core.md",
        "turns end in promises instead of results — strengthen the no-early-stop "
        "steering (and only if steering fails, escalate to a structural check)"),
    "no_doom_loop": (
        "architecture-defect", "code", "",
        "repeated identical steps need a circuit breaker — a prompt can only "
        "ask nicely; the breaker must be code"),
    "no_raw_ids": (
        "missing-context", "code", "tools/people.py",
        "raw users/<id> shown means the roster wasn't injected/consulted — a "
        "context-construction gap"),
    "budget_duration": (
        "architecture-defect", "code", "",
        "budget overruns are bounded by enforcement, not by asking"),
    "budget_cost": (
        "architecture-defect", "code", "",
        "budget overruns are bounded by enforcement, not by asking"),
    "nonempty_output": (
        "tool-defect", "code", "",
        "empty outputs cluster with runner/caller failures; verify the run "
        "path before the prompt"),
    "no_error": (
        "tool-defect", "code", "",
        "hard errors are code/tool failures first"),
    # wiring findings
    "agent_md_exists": (
        "architecture-defect", "code", ".claude/agents/",
        "an invoked agent without a definition file is a wiring defect"),
    "slug_map_covers_agent": (
        "tool-defect", "code", "brain/claude_runner.py",
        "the alias table misses an agent name verticals actually pass"),
    "has_call_site": (
        "architecture-defect", "code", "conductor/orchestrator.py",
        "capability described but unreachable — wire it or retire it"),
    "journal_field_mismatch": (
        "tool-defect", "code", "learning/self_improve.py",
        "consumers read a field the journal never writes — the learning loop "
        "runs on empty strings"),
    "artifact_clean": (
        "missing-context", "code", "verticals/day.py",
        "the artifact carries the model's complaint about its input — fix the "
        "input pipeline"),
    "unobservable": (
        "architecture-defect", "instrumentation", "",
        "decisions that are recorded nowhere cannot be reviewed, coached, or "
        "trusted — add the decision log"),
}

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def _fingerprint(agent: str, check: str) -> str:
    return f"{agent}:{check}"


def _issue_id(fingerprint: str) -> str:
    return "ci-" + hashlib.sha1(fingerprint.encode()).hexdigest()[:8]


def _classify(check: str) -> tuple[str, str, str, str]:
    if check.startswith("judge:"):
        # advisory judgments never drive a fix alone; they annotate issues
        return ("prompt-defect", "prompt", "", "advisory judge signal")
    return _CLASS_RULES.get(check, (
        "tool-defect", "code", "",
        "unmapped check — conservative default is the code path, never a "
        "prompt patch on speculation"))


def build_issues(verdicts: list[dict], static_findings: list[dict],
                 rubrics: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Cluster failing verdicts + static findings into issues.

    Returns (issues, transients). Transients are below their rubric's
    min_occurrences: recorded, never filed (skip-if-nothing).
    """
    now = datetime.now(timezone.utc).isoformat()

    clusters: dict[str, list[dict]] = {}
    for v in verdicts:
        if v.get("ok") is False and not str(v.get("check", "")).startswith("judge:"):
            clusters.setdefault(_fingerprint(v["agent"], v["check"]), []).append(v)

    # advisory judge failures attach as annotations to same-agent issues
    judge_notes: dict[str, list[str]] = {}
    for v in verdicts:
        if v.get("ok") is False and str(v.get("check", "")).startswith("judge:"):
            judge_notes.setdefault(v["agent"], []).append(
                f"{v['check']}: {v.get('evidence','')}")

    issues, transients = [], []
    for fp, members in clusters.items():
        agent, check = fp.split(":", 1)
        rubric = rubrics.get(agent, {})
        min_occ = int(rubric.get("min_occurrences", 2))
        if len(members) < min_occ:
            transients.append({"fingerprint": fp, "count": len(members),
                               "needed": min_occ, "evidence": members[:2]})
            continue
        rca_class, channel, target, why = _classify(check)
        severity = min((m.get("severity", "medium") for m in members),
                       key=lambda s: _SEV_ORDER.get(s, 1))
        issues.append({
            "id": _issue_id(fp),
            "fingerprint": fp,
            "title": f"[{agent}] {check}: {members[-1].get('evidence','')[:90]}",
            "agent": agent,
            "check": check,
            "rca_class": rca_class,
            "fix_channel": channel,
            "fix_target": target,
            "rca_why": why,
            "severity": severity,
            "count": len(members),
            "episode_ids": [m.get("episode_id", "") for m in members][:10],
            "evidence": [
                {"episode_id": m.get("episode_id", ""), "episode_ts": m.get("episode_ts", ""),
                 "evidence": m.get("evidence", ""), "stream": m.get("stream", "")}
                for m in members[:5]],
            "judge_notes": judge_notes.get(agent, [])[:3],
            "created": now,
        })

    for f in static_findings:
        agent = f.get("agent", "?")
        check = f.get("check", "wiring")
        fp = _fingerprint(agent, check) + ":" + hashlib.sha1(
            str(f.get("summary", "")).encode()).hexdigest()[:6]
        rca_class, channel, target, why = _classify(check)
        issues.append({
            "id": _issue_id(fp),
            "fingerprint": fp,
            "title": f"[{agent}] {f.get('summary','')[:110]}",
            "agent": agent,
            "check": check,
            "rca_class": rca_class,
            "fix_channel": channel,
            "fix_target": target or (f.get("evidence") or [""])[0],
            "rca_why": why,
            "severity": f.get("severity", "medium"),
            "count": 1,
            "episode_ids": [],
            "evidence": [{"evidence": e, "stream": "wiring"} for e in (f.get("evidence") or [])[:5]],
            "judge_notes": [],
            "created": now,
        })

    issues.sort(key=lambda i: (_SEV_ORDER.get(i["severity"], 1), -i["count"]))
    return issues, transients


def log_transients(transients: list[dict]) -> None:
    """Below-threshold failures still leave a trace (the journal is where
    recurrence is counted), they just don't become kanban issues yet."""
    if not transients:
        return
    try:
        from learning import error_journal
        for t in transients:
            error_journal.log_error(
                agent=t["fingerprint"].split(":", 1)[0],
                session_id=f"coach-{datetime.now().strftime('%Y%m%d')}",
                task_summary=f"coaching transient: {t['fingerprint']}",
                error_type="coaching_transient",
                error_message=json.dumps(t["evidence"][:1])[:400],
                signal_trace=f"count {t['count']} < min {t['needed']}",
                what_should_have_happened="recurs -> becomes a kanban issue",
                pattern_name=f"coach-{t['fingerprint']}",
            )
    except Exception as e:
        logger.debug("transient logging failed: %s", e)
