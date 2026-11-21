"""Self-evolving loop -- Sentinel keeps itself current, but never auto-applies.

The scheduled "keep-current" loop:

  1. RESEARCH  -- shell the sherlock research skill for what changed in the
     LLM / agent-framework landscape this week (new models, prices, better
     practices). This blocks 15-25 minutes; failure degrades gracefully.
  2. PROPOSE   -- run a Claude Code "act" agent over REPO_ROOT that reads the
     research + brain/model_router.py and, IF there is a concrete worthwhile
     change, writes it on a branch `sentinel/evolve-<date>`.
  3. EVAL GATE -- run_evals(); only proceed if the candidate is at least as
     good as the baseline.
  4. PROPOSE via broker.propose(action_type="self_modify", ...,
     criticality_hint="critical", reversibility="irreversible"). NEVER
     auto-merged -- a human must approve every self-modification.

The eval checker (check_assertion/check_case/grade_outputs) is the durable,
unit-testable core: given an output string and a list of assertions, it decides
pass/fail deterministically. run_candidate() is the real runner: it feeds each
case through the conductor agent read-only and grades the outputs, gating the
self-modify proposal against a persisted baseline (EVALS_DIR/'baseline.json').
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.paths import EVALS_DIR, REPO_ROOT

logger = logging.getLogger("evolve")

# Path to the sherlock research skill CLI (installed under the user's skills).
SHERLOCK_SCRIPT = Path.home() / ".claude" / "skills" / "sherlock-research" / "scripts" / "sherlock.py"

RESEARCH_QUESTION = (
    "What changed in the LLM/agent-framework landscape this week that Sentinel "
    "should adopt (new models, prices, better practices)? Focus on concrete, "
    "actionable changes to model selection and cost."
)

# Deep research blocks 15-25 min; give it a generous ceiling.
RESEARCH_TIMEOUT_SECONDS = 40 * 60
INVOKE_TIMEOUT_SECONDS = 120

# Assertion types the checker understands.
VALID_ASSERTION_TYPES = {
    "exact",
    "contains",
    "not_contains",
    "regex",
    "not_regex",
    "min_length",
    "max_length",
}


# ---------------------------------------------------------------------------
# EVAL GATE
# ---------------------------------------------------------------------------

def check_assertion(output: str, assertion: dict) -> tuple[bool, str]:
    """Check a single assertion against an output string.

    Returns (passed, reason). `reason` is empty on pass, else a short message.

    assertion = {"type": <one of VALID_ASSERTION_TYPES>, "value": <...>}
      exact        -> output equals value
      contains     -> value is a substring of output
      not_contains -> value is NOT a substring of output
      regex        -> re.search(value, output) matches
      min_length   -> len(output) >= int(value)
      max_length   -> len(output) <= int(value)
    """
    a_type = assertion.get("type", "")
    value = assertion.get("value")
    text = "" if output is None else str(output)

    if a_type == "exact":
        ok = text == str(value)
        return ok, "" if ok else f"expected exact {value!r}"

    if a_type == "contains":
        ok = str(value) in text
        return ok, "" if ok else f"missing substring {value!r}"

    if a_type == "not_contains":
        ok = str(value) not in text
        return ok, "" if ok else f"unexpected substring {value!r}"

    if a_type == "regex":
        try:
            ok = re.search(str(value), text) is not None
        except re.error as e:
            return False, f"bad regex {value!r}: {e}"
        return ok, "" if ok else f"no regex match {value!r}"

    if a_type == "not_regex":
        try:
            ok = re.search(str(value), text) is None
        except re.error as e:
            return False, f"bad regex {value!r}: {e}"
        return ok, "" if ok else f"unexpected regex match {value!r}"

    if a_type == "min_length":
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            return False, f"min_length value not an int: {value!r}"
        ok = len(text) >= threshold
        return ok, "" if ok else f"len {len(text)} < min {threshold}"

    if a_type == "max_length":
        try:
            threshold = int(value)
        except (TypeError, ValueError):
            return False, f"max_length value not an int: {value!r}"
        ok = len(text) <= threshold
        return ok, "" if ok else f"len {len(text)} > max {threshold}"

    return False, f"unknown assertion type {a_type!r}"


def check_case(output: str, assertions: list[dict]) -> dict:
    """Check ALL assertions for one case against one output.

    Returns {"passed": bool, "failures": [reasons], "total": n, "passed_count": k}.
    A case passes only if every assertion passes.
    """
    failures: list[str] = []
    for assertion in assertions or []:
        ok, reason = check_assertion(output, assertion)
        if not ok:
            failures.append(reason)
    total = len(assertions or [])
    return {
        "passed": not failures,
        "failures": failures,
        "total": total,
        "passed_count": total - len(failures),
    }


def _starter_cases() -> list[dict]:
    """Two example eval cases with binary assertions."""
    return [
        {
            "id": "github-triage-classification",
            "vertical": "github",
            "input": "Issue: 'App crashes with NullPointerException on login'. Classify severity.",
            "assertions": [
                {"type": "contains", "value": "bug"},
                {"type": "not_contains", "value": "feature request"},
                {"type": "min_length", "value": 3},
            ],
        },
        {
            "id": "inbox-voice-check",
            "vertical": "inbox",
            "input": "Draft a reply declining a meeting, in the user's concise voice.",
            "assertions": [
                {"type": "not_contains", "value": "As an AI language model"},
                {"type": "regex", "value": "(?i)thank"},
                {"type": "max_length", "value": 1200},
            ],
        },
    ]


def run_evals(eval_path=None) -> dict:
    """Load the eval suite and run it (candidate runner stubbed for now).

    If no eval.json exists, write a small starter suite (2 example cases) and
    return a note -- the real candidate runner is wired later. The assertion
    CHECKER (check_assertion / check_case) is fully implemented and testable
    independent of any candidate.
    """
    path = Path(eval_path) if eval_path else (EVALS_DIR / "eval.json")

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = _starter_cases()
        path.write_text(json.dumps(cases, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("No eval.json -- wrote starter suite (%d cases) to %s", len(cases), path)
        return {
            "pass_rate": None,
            "note": "starter evals written, wire real candidate runner later",
            "eval_path": str(path),
            "num_cases": len(cases),
        }

    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load eval suite %s: %s", path, e)
        return {"pass_rate": None, "note": f"failed to load eval suite: {e}", "eval_path": str(path)}

    if not isinstance(cases, list) or not cases:
        return {"pass_rate": None, "note": "eval suite is empty", "eval_path": str(path)}

    # No candidate runner yet: we can validate the suite and exercise the
    # checker, but we cannot produce candidate outputs to grade. Report that
    # the runner is not wired rather than faking a pass_rate.
    return {
        "pass_rate": None,
        "note": "candidate runner not wired yet; assertion checker is available via check_case()",
        "eval_path": str(path),
        "num_cases": len(cases),
    }


def _load_baseline() -> float | None:
    """Load the persisted eval baseline pass_rate (EVALS_DIR/'baseline.json')."""
    path = EVALS_DIR / "baseline.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("pass_rate")
    except (json.JSONDecodeError, OSError, AttributeError):
        return None


def _save_baseline(pass_rate: float) -> None:
    """Persist the eval baseline pass_rate (EVALS_DIR/'baseline.json')."""
    path = EVALS_DIR / "baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pass_rate": pass_rate, "recorded_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )


def _load_cases(eval_path=None) -> list[dict]:
    """Load the eval suite (list of cases) from EVALS_DIR/'eval.json'.

    Returns an empty list if the file is missing or malformed.
    """
    path = Path(eval_path) if eval_path else (EVALS_DIR / "eval.json")
    if not path.exists():
        return []
    try:
        cases = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load eval suite %s: %s", path, e)
        return []
    return cases if isinstance(cases, list) else []


def grade_outputs(outputs: dict[str, str], cases: list[dict] | None = None) -> dict:
    """Grade a mapping of {case_id: output_string} against the eval cases.

    `cases` defaults to the loaded eval suite (EVALS_DIR/'eval.json') so the
    candidate runner can call grade_outputs(outputs) directly.
    Returns {"pass_rate": float, "results": [{id, passed, failures}], ...}.
    """
    if cases is None:
        cases = _load_cases()
    results = []
    passed = 0
    for case in cases:
        cid = case.get("id", "")
        output = outputs.get(cid, "")
        graded = check_case(output, case.get("assertions", []))
        if graded["passed"]:
            passed += 1
        results.append({
            "id": cid,
            "vertical": case.get("vertical", ""),
            "passed": graded["passed"],
            "failures": graded["failures"],
        })
    total = len(cases)
    return {
        "pass_rate": (passed / total) if total else None,
        "passed": passed,
        "total": total,
        "results": results,
    }


async def run_candidate(eval_path=None, model=None) -> dict:
    """Run the eval suite against a live candidate and grade the outputs.

    Loads EVALS_DIR/'eval.json' (list of {id, vertical, input, assertions}),
    runs each case through the conductor agent read-only, collects the result
    text keyed by case id, then grades via grade_outputs(). Each case is guarded
    so one bad run doesn't sink the batch. Returns
    {pass_rate, passed, total, per_case}.
    """
    from brain.claude_runner import run_claude

    cases = _load_cases(eval_path)
    if not cases:
        return {"pass_rate": None, "passed": 0, "total": 0, "per_case": {}}

    outputs: dict[str, str] = {}
    per_case: dict[str, str] = {}
    for case in cases:
        cid = case.get("id", "")
        final_text = ""
        try:
            async for event in run_claude(
                case.get("input", ""),
                agent="conductor",
                tier="read",
                allowed_tools=["Read"],
                model=model,
                max_turns=0,
            ):
                if event.type in ("assistant", "result") and event.content:
                    final_text = event.content
        except Exception as e:  # one bad case must not sink the batch
            logger.warning("run_candidate: case %s failed: %s", cid, e)
            per_case[cid] = f"__error__: {e}"
        else:
            per_case[cid] = final_text
        outputs[cid] = final_text

    graded = grade_outputs(outputs, cases)
    return {
        "pass_rate": graded["pass_rate"],
        "passed": graded["passed"],
        "total": graded["total"],
        "per_case": per_case,
    }


# ---------------------------------------------------------------------------
# RESEARCH
# ---------------------------------------------------------------------------

async def _research() -> dict:
    """Shell the sherlock skill: invoke -> parse task_id -> poll (blocks).

    Returns {"status": "ok", "report": <text>} or {"status": "research_failed", ...}.
    """
    if not SHERLOCK_SCRIPT.exists():
        return {"status": "research_failed", "reason": f"sherlock script not found: {SHERLOCK_SCRIPT}"}

    # 1) invoke -> prints JSON {task_id, ...}
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SHERLOCK_SCRIPT), "invoke", RESEARCH_QUESTION,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=INVOKE_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, OSError) as e:
        return {"status": "research_failed", "reason": f"invoke failed: {e}"}

    if proc.returncode != 0:
        return {
            "status": "research_failed",
            "reason": f"invoke exited {proc.returncode}: {err.decode('utf-8', 'replace')[:300]}",
        }

    try:
        invoke_resp = json.loads(out.decode("utf-8", "replace"))
        task_id = invoke_resp.get("task_id")
    except (json.JSONDecodeError, AttributeError):
        task_id = None
    if not task_id:
        return {"status": "research_failed", "reason": "no task_id from invoke"}

    # 2) poll <task_id> -- blocks 15-25 min, prints report to stdout.
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SHERLOCK_SCRIPT), "poll", str(task_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RESEARCH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"status": "research_failed", "reason": "poll timed out"}
    except OSError as e:
        return {"status": "research_failed", "reason": f"poll failed: {e}"}

    report = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not report:
        return {
            "status": "research_failed",
            "reason": f"poll exited {proc.returncode} with empty/short report",
        }

    return {"status": "ok", "report": report, "task_id": task_id}


# ---------------------------------------------------------------------------
# PROPOSE (Claude Code agent writes a candidate branch)
# ---------------------------------------------------------------------------

async def _propose_candidate(research_report: str, branch: str) -> dict:
    """Run a Claude Code "act" agent to write a candidate change on `branch`.

    Reads the research + brain/model_router.py and, IF there is a concrete
    worthwhile change, creates the branch and edits the router. Returns a dict
    summarizing what the agent reported. Never merges.
    """
    from brain.claude_runner import run_claude

    prompt = (
        "You are Sentinel's self-evolution agent. Below is fresh research on the "
        "LLM/agent-framework landscape. Read brain/model_router.py (the TIER_MODELS "
        "table and ModelProfile entries).\n\n"
        "TASK: ONLY if the research names a concrete, worthwhile change (a newly "
        "shipped model that is strictly better/cheaper for a tier, or a corrected "
        "price), then:\n"
        f"  1. Create a git branch named `{branch}`.\n"
        "  2. Make the minimal edit to brain/model_router.py.\n"
        "  3. Commit on that branch (do NOT merge, do NOT touch main/master).\n"
        "If there is NO clearly worthwhile change, make NO edits and say so.\n\n"
        "End your final message with one line: CHANGE_MADE=yes or CHANGE_MADE=no, "
        "and a one-sentence rationale.\n\n"
        "=== RESEARCH ===\n"
        f"{research_report[:12000]}\n"
    )

    final_text = ""
    error = ""
    async for event in run_claude(
        prompt=prompt,
        agent="code-bot",
        working_dir=str(REPO_ROOT),
        tier="act",
        max_turns=30,
    ):
        if event.type in ("assistant", "result"):
            if event.content:
                final_text = event.content
        elif event.type == "error":
            error = event.content

    if error and not final_text:
        return {"change_made": False, "error": error, "branch": branch}

    change_made = "change_made=yes" in final_text.lower()
    return {
        "change_made": change_made,
        "summary": final_text[-1000:],
        "branch": branch,
    }


# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------

async def run_evolution(broker, dry_run: bool = False) -> dict:
    """The scheduled keep-current loop. Never auto-merges self-modifications.

    Returns a status dict at each early-exit. When dry_run is True, the final
    broker.propose is skipped and the would-be proposal is returned instead.
    """
    started = datetime.now(timezone.utc).isoformat()

    # 1) RESEARCH (blocks 15-25 min)
    research = await _research()
    if research.get("status") != "ok":
        return {"status": "research_failed", "detail": research.get("reason", ""), "started": started}
    report = research["report"]

    # 2) PROPOSE candidate branch via a Claude Code agent.
    branch = f"sentinel/evolve-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"
    proposal = await _propose_candidate(report, branch)
    if not proposal.get("change_made"):
        return {
            "status": "no_change_worthwhile",
            "branch": branch,
            "agent_summary": proposal.get("summary", ""),
            "started": started,
        }

    # 3) EVAL GATE -- run the real candidate and compare to a persisted baseline.
    evals = await run_candidate()
    pass_rate = evals.get("pass_rate")
    baseline = _load_baseline()
    if pass_rate is not None:
        if baseline is None:
            # First run: record the current pass_rate as baseline and proceed.
            _save_baseline(pass_rate)
            baseline = pass_rate
        elif pass_rate < baseline:
            return {
                "status": "eval_regression",
                "branch": branch,
                "pass_rate": pass_rate,
                "baseline": baseline,
                "started": started,
            }
    # If pass_rate is None there were no gradeable cases; we do NOT let a missing
    # gate auto-approve a self-modification -- the broker holds it as
    # critical/irreversible regardless, and a human must approve.

    description = f"Self-modify brain/model_router.py on branch {branch} (from weekly evolution research)."
    details = {
        "branch": branch,
        "file": "brain/model_router.py",
        "agent_summary": proposal.get("summary", ""),
        "evals": evals,
        "baseline": baseline,
    }
    reasoning = (
        "Weekly evolution research surfaced a concrete model/pricing change. A candidate "
        "branch was written by the code agent. Holding for human approval -- self-modifications "
        "are never auto-applied."
    )

    if dry_run:
        return {
            "status": "would_propose",
            "branch": branch,
            "proposal": {
                "action_type": "self_modify",
                "description": description,
                "details": details,
                "reasoning": reasoning,
                "criticality_hint": "critical",
                "reversibility": "irreversible",
                "project": "sentinel",
                "best_effort_plan": "hold — never auto-apply self-modifications",
            },
            "started": started,
        }

    # 4) PROPOSE via broker -- critical + irreversible, held for a human.
    result = await broker.propose(
        action_type="self_modify",
        description=description,
        details=details,
        reasoning=reasoning,
        project="sentinel",
        criticality_hint="critical",
        reversibility="irreversible",
        best_effort_plan="hold — never auto-apply self-modifications",
        source="evolve",
    )

    return {"status": "proposed", "branch": branch, "broker_result": result, "started": started}
