"""Verify — the fix is real only when the check that caught the failure passes.

Protocol (research-mandated, all deterministic):
  1. Failing episodes become a REGRESSION CASE before any fix is written
     (pytest for code-reachable defects; evolve-style llm cases for prompt
     behavior).
  2. BEFORE: the case must fail/reproduce on the incumbent code.
  3. AFTER: the case must pass in the candidate worktree, AND the full test
     suite shows no new failures vs the incumbent baseline.
  4. Anti-gaming: the candidate diff may not touch rubrics, checks, eval
     files, or other issues' repro tests (unless the issue class is
     spec-wrong, whose whole point is changing the definition).
  5. The fixer's "I fixed it" is never an input — only these results are.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from config.paths import REPO_ROOT

logger = logging.getLogger("coach.verify")

REPRO_DIR_NAME = "tests/coaching_repro"

# files a fix may never modify (it MAY add its own new repro test)
_PROTECTED_PATTERNS = (
    r"^coaching/rubrics/", r"^coaching/checks\.py$", r"^coaching/verify\.py$",
    r"^evals/", r"^tests/coaching_repro/(?!test_{issue_id})",
)


def run_pytest(target: str, cwd: str, timeout: int = 600) -> dict:
    """Run pytest on a target path. Returns {passed, exit_code, tail}."""
    try:
        res = subprocess.run(
            ["python3", "-m", "pytest", target, "-x", "-q", "--no-header"],
            capture_output=True, text=True, cwd=cwd, timeout=timeout)
        tail = (res.stdout + "\n" + res.stderr)[-1500:]
        return {"passed": res.returncode == 0, "exit_code": res.returncode, "tail": tail}
    except subprocess.TimeoutExpired:
        return {"passed": False, "exit_code": -1, "tail": "pytest timed out"}
    except Exception as e:
        return {"passed": False, "exit_code": -1, "tail": f"pytest failed to run: {e}"}


def suite_failures(cwd: str, timeout: int = 900) -> set[str] | None:
    """Failing test ids for the whole suite — the regression baseline.
    (Same idea as tools/test_runner's pre-fix baseline: pre-existing failures
    don't block, NEW failures do.)

    Returns None when the suite FAILED TO RUN (timeout, crash, collection
    error) — 'could not measure' must never be mistaken for 'zero failures',
    or a fix that hangs the suite sails through the gate with false evidence.
    pytest exit codes: 0 = all passed, 1 = tests failed, anything else =
    the run itself broke."""
    try:
        res = subprocess.run(
            ["python3", "-m", "pytest", "tests", "-q", "--no-header", "--tb=no",
             "--ignore", REPRO_DIR_NAME],
            capture_output=True, text=True, cwd=cwd, timeout=timeout)
        if res.returncode not in (0, 1):
            logger.warning("suite run broke (exit %s): %s", res.returncode,
                           (res.stdout + res.stderr)[-300:])
            return None
        failed = set()
        for line in (res.stdout or "").splitlines():
            m = re.match(r"(FAILED|ERROR)\s+(\S+)", line.strip())
            if m:
                failed.add(m.group(2).split("[")[0])
        return failed
    except Exception as e:
        logger.warning("suite baseline failed to run: %s", e)
        return None


def regression_gate(base_failures: set[str] | None, cwd: str) -> dict:
    """No NEW failures vs the incumbent baseline. A suite that cannot be
    measured (before or after) FAILS the gate — unknown is not green."""
    after = suite_failures(cwd)
    if after is None or base_failures is None:
        return {"passed": False, "new_failures": [],
                "pre_existing": [],
                "error": "suite failed to run — cannot prove no regressions"}
    new = sorted(after - base_failures)
    return {"passed": not new, "new_failures": new,
            "pre_existing": sorted(base_failures & after)}


def diff_files(worktree: str, base_ref: str = "HEAD") -> list[str]:
    # --no-renames: with rename detection on, `git mv coaching/checks.py X`
    # reports only the NEW path, letting a renamed measuring-stick file slip
    # past the protected patterns. Show both sides of every rename.
    try:
        res = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", f"{base_ref}...HEAD"],
            capture_output=True, text=True, cwd=worktree, timeout=30)
        files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
        if not files:  # branch may not have diverged via merge-base; plain diff
            res = subprocess.run(["git", "diff", "--name-only", "--no-renames", base_ref],
                                 capture_output=True, text=True, cwd=worktree, timeout=30)
            files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
        return files
    except Exception:
        return []


def anti_gaming(changed: list[str], issue: dict) -> dict:
    """A fix that edits the measuring stick is not a fix. spec-wrong issues are
    the one legitimate exception — their target IS the spec — and even then the
    human sees the diff."""
    if issue.get("rca_class") == "spec-wrong":
        return {"passed": True, "violations": [], "note": "spec-wrong: measure edits allowed, human reviews"}
    raw_id = issue.get("id", "x")
    # repro files use underscores (test_ci_abc123.py); accept either form
    issue_id = f"(?:{re.escape(raw_id)}|{re.escape(raw_id.replace('-', '_'))})"
    violations = []
    for f in changed:
        for pat in _PROTECTED_PATTERNS:
            if re.match(pat.format(issue_id=issue_id), f):
                violations.append(f)
                break
    return {"passed": not violations, "violations": violations}


def _is_test_file(path: str) -> bool:
    p = path.strip()
    return (p.startswith("tests/") or "/tests/" in p
            or p.rsplit("/", 1)[-1].startswith("test_")
            or p.endswith("_test.py"))


def feature_gate(changed: list[str], issue: dict) -> dict:
    """Capability requests (fix_channel='feature') prove themselves with an
    acceptance test that FAILS before and PASSES after — exactly like a defect
    repro. But a feature has one gaming route a defect repro does not: the
    agent can make its own acceptance test pass by weakening the *test* while
    shipping no real implementation. The defect path is immune because its
    repro must fail then pass on UNCHANGED production code; a feature has no
    such incumbent code, so we require it explicitly here:

      the diff MUST change at least one non-test implementation file.

    A test-only feature diff is not a capability — it's a green light with
    nothing behind it. (Everything anti_gaming protects still applies; this is
    an ADDITIONAL gate, run only for feature issues.)
    """
    impl = [f for f in changed if not _is_test_file(f)]
    if not impl:
        return {"passed": False, "impl_files": [],
                "reason": ("feature diff touches only tests — a capability must "
                           "have a real implementation, not just a passing test")}
    return {"passed": True, "impl_files": impl}


# ─────────────────────── llm repro cases (prompt-defect fixes) ───────────────────────

def make_llm_cases(issue: dict, episodes_by_id: dict[str, dict]) -> list[dict]:
    """Derive evolve-style cases from the issue's failing episodes: same input,
    assertion derived from the failed check. Only checks with a clean
    output-level assertion get llm cases."""
    check = issue.get("check", "")
    assertion_map = {
        "no_placeholder_sources": [{"type": "not_contains", "value": "example.com"},
                                   {"type": "not_contains", "value": "[source]"}],
        "sources_present": [{"type": "regex", "value": r"https?://"}],
        # match the promise-tail pattern the check itself uses; not_regex keeps
        # multi-line outputs graded on content (a bare ^…$ regex without re.M
        # fails on EVERY multi-line answer regardless of content)
        "answer_not_promise": [{"type": "not_regex",
                                "value": r"(?i)\b(i'?ll (do|get|start|look|check)|"
                                         r"let me (start|work|look|check)|"
                                         r"give me a (moment|minute))\b[^\n]{0,80}$"}],
        "draft_nonempty_or_skip": [{"type": "min_length", "value": 2}],
    }
    asserts = assertion_map.get(check)
    if not asserts:
        return []
    cases = []
    for eid in issue.get("episode_ids", [])[:3]:
        e = episodes_by_id.get(eid)
        if not e or not str(e.get("input", "")).strip():
            continue
        cases.append({"id": f"{issue['id']}-{eid[-6:]}", "agent": issue.get("agent", ""),
                      "input": str(e["input"])[:4000], "assertions": asserts})
    return cases


async def run_llm_cases(cases: list[dict], cwd: str, agent: str = "conductor",
                        max_turns: int = 6) -> dict:
    """Replay llm cases through the (possibly candidate) code in `cwd`, grade
    with the shared evolve checker. Bounded and guarded."""
    from brain.claude_runner import run_claude
    from learning.evolve import check_case

    results, passed = {}, 0
    for case in cases:
        final = ""
        try:
            async for ev in run_claude(case["input"], agent=agent or "conductor",
                                       tier="read", working_dir=cwd,
                                       max_turns=max_turns,
                                       episode_source="coach-run"):
                if ev.type in ("assistant", "result") and ev.content:
                    final = ev.content
        except Exception as e:
            final = f"__error__: {e}"
        graded = check_case(final, case.get("assertions", []))
        results[case["id"]] = {"passed": graded["passed"],
                               "failures": graded["failures"],
                               "output_head": final[:300]}
        if graded["passed"]:
            passed += 1
    return {"passed": passed, "total": len(cases), "per_case": results,
            "all_passed": passed == len(cases) and len(cases) > 0}
