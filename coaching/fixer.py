"""Fixer — turns one kanban issue into a verified, approval-gated fix.

The flow (every step Python-verified, no step trusts the model's narration):

  1. pick the top backlog issue; branch `sentinel/coach/<id>` is created
     DETERMINISTICALLY in Python in an isolated worktree (the agent never
     creates branches — RCA law from the autopilot hardening).
  2. PHASE 1 — repro first: code-bot writes ONLY a failing test
     (tests/coaching_repro/test_<id>.py). Python runs it: it MUST fail.
     That failing run is the BEFORE evidence. (For prompt-defect issues an
     evolve-style llm replay is the before/after instead.)
  3. PHASE 2 — minimal fix: code-bot fixes; Python re-runs the repro (MUST
     pass now), then the full suite (no NEW failures vs the incumbent
     baseline), then the anti-gaming diff check, then a fresh-context
     review-bot pass (empty review = review FAILED, never clean).
  4. broker.propose(self_modify, critical) with all evidence in the card.
     Held for the owner. On approval the executor merges the branch — git is the
     rollback substrate. Nothing is ever auto-applied.

Bounded: one issue per cycle, two agent runs per phase max, hard timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config.paths import REPO_ROOT, SENTINEL_HOME
from coaching import kanban, verify

logger = logging.getLogger("coach.fixer")

WORKTREES_DIR = SENTINEL_HOME / "worktrees"
MAX_AGENT_TURNS = 40


def _git(cwd: str | Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        res = subprocess.run(["git", *args], capture_output=True, text=True,
                             cwd=str(cwd), timeout=timeout)
        return res.returncode, (res.stdout + res.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _setup_worktree(issue_id: str) -> tuple[str, str, str] | None:
    """Create branch + worktree deterministically. Returns (branch, path, base_sha)."""
    branch = f"sentinel/coach/{issue_id}"
    path = WORKTREES_DIR / f"coach-{issue_id}"
    rc, base_sha = _git(REPO_ROOT, "rev-parse", "HEAD")
    if rc != 0:
        logger.warning("worktree setup: no HEAD (%s)", base_sha)
        return None
    if path.exists():
        _git(REPO_ROOT, "worktree", "remove", "--force", str(path))
    rc, out = _git(REPO_ROOT, "worktree", "add", "-B", branch, str(path), "HEAD")
    if rc != 0:
        logger.warning("worktree add failed: %s", out)
        return None
    return branch, str(path), base_sha.strip()


async def _run_codebot(prompt: str, cwd: str) -> str:
    from brain.claude_runner import run_claude
    final = ""
    async for ev in run_claude(prompt, agent="code-bot", tier="act",
                               working_dir=cwd, max_turns=MAX_AGENT_TURNS,
                               task_type="coding", episode_source="coach-run"):
        if ev.type in ("assistant", "result") and ev.content:
            final = ev.content
    return final


def _commits_since(cwd: str, base_sha: str) -> int:
    rc, out = _git(cwd, "rev-list", "--count", f"{base_sha}..HEAD")
    try:
        return int(out.strip()) if rc == 0 else 0
    except ValueError:
        return 0


def _issue_brief(issue: dict) -> str:
    ev = "\n".join(f"- [{e.get('stream','')}] {e.get('evidence','')[:200]}"
                   for e in issue.get("evidence", [])[:5])
    brief = (
        f"COACHING ISSUE {issue['id']} (RCA class: {issue['rca_class']}, "
        f"severity {issue['severity']}, seen {issue.get('count',1)}x)\n"
        f"Agent under coaching: {issue['agent']}\n"
        f"Failed check: {issue['check']}\n"
        f"Root-cause reasoning: {issue.get('rca_why','')}\n"
        f"Fix channel: {issue.get('fix_channel','code')} — likely target: "
        f"{issue.get('fix_target') or 'locate it from the evidence'}\n"
        f"Evidence from real runs:\n{ev}\n"
    )
    plan = (issue.get("plan") or {}).get("text", "")
    if plan:
        brief += (f"\nRESEARCHED PLAN (by {issue['plan'].get('by','analyst')} — follow it "
                  f"unless the code contradicts it):\n{plan[:2500]}\n")
    return brief


async def fix_one(issue: dict, broker, dry_run: bool = False) -> dict:
    """Run the full repro→fix→verify→propose flow for one issue."""
    issue_id = issue["id"]
    wt = _setup_worktree(issue_id)
    if not wt:
        return {"status": "worktree_failed", "issue": issue_id}
    branch, wt_path, base_sha = wt
    kanban.move(issue_id, "in_progress", fix={"branch": branch, "worktree": wt_path,
                                              "base_sha": base_sha})

    repro_rel = f"{verify.REPRO_DIR_NAME}/test_{issue_id.replace('-', '_')}.py"
    channel = issue.get("fix_channel")
    use_llm_replay = channel == "prompt"
    is_feature = channel == "feature"
    before: dict = {}
    llm_cases: list[dict] = []

    if use_llm_replay:
        # prompt-behavior defects: before/after is an episode replay, not pytest
        from coaching.episodes import collect_episodes
        eps = {e["id"]: e for e in collect_episodes(since_hours=24 * 21)}
        llm_cases = verify.make_llm_cases(issue, eps)
        if not llm_cases:
            # no derivable repro -> a fix attempt would face an unsatisfiable
            # gate; never spend a code-bot run against it. Park for a human.
            kanban.move(issue_id, "backlog",
                        fix={"note": "no replayable repro cases derivable from evidence"},
                        attempts=int(issue.get("attempts", 0)) + 1)
            _teardown(wt_path)
            return {"status": "no_repro_cases", "issue": issue_id}
        before = await verify.run_llm_cases(llm_cases, str(REPO_ROOT),
                                            agent=issue.get("agent", "conductor"))
        if before.get("all_passed"):
            kanban.move(issue_id, "backlog", fix={"note": "not reproducible in replay"},
                        attempts=int(issue.get("attempts", 0)) + 1)
            _teardown(wt_path)
            return {"status": "not_reproducible", "issue": issue_id}
    else:
        # PHASE 1 — failing test first. Defect: a repro of the bug. Feature: an
        # acceptance test that encodes the REQUESTED capability. Both must fail
        # now (defect present / capability absent) and pass once built — same
        # BEFORE evidence, same anti-blind-fix guard.
        if is_feature:
            p1 = (
                _issue_brief(issue) +
                f"\nPHASE 1 OF 2 — WRITE THE ACCEPTANCE TEST ONLY, DO NOT BUILD YET.\n"
                f"This is a NEW CAPABILITY request, not a bug. Write ONE focused pytest "
                f"file at {repro_rel} that asserts the requested behavior described above "
                f"as a black-box spec: import the real modules and call the public "
                f"interface the capability WILL expose. It MUST fail right now because "
                f"the capability does not exist yet (ImportError/AttributeError/assert). "
                f"Assert real observable behavior — never `assert True`, never catch-and-"
                f"pass the missing-attribute error. Then `git add -A && git commit -m "
                f"'coach({issue_id}): acceptance test'`. Do NOT implement the capability yet."
            )
        else:
            p1 = (
                _issue_brief(issue) +
                f"\nPHASE 1 OF 2 — REPRODUCE ONLY, DO NOT FIX.\n"
                f"Write ONE focused pytest file at {repro_rel} that reproduces this defect "
                f"(it must FAIL on the current code for the defect reason, and will pass once "
                f"fixed). Import the real modules; no mocks of the code under test's own "
                f"logic. Then `git add -A && git commit -m 'coach({issue_id}): failing repro'`. "
                f"Do NOT modify any other file. Do NOT fix the bug yet."
            )
        await _run_codebot(p1, wt_path)
        if _commits_since(wt_path, base_sha) < 1 or not (Path(wt_path) / repro_rel).exists():
            note = ("phase1 produced no acceptance test" if is_feature
                    else "phase1 produced no repro test")
            kanban.move(issue_id, "backlog", fix={"note": note},
                        attempts=int(issue.get("attempts", 0)) + 1)
            _teardown(wt_path)
            return {"status": "repro_not_written", "issue": issue_id}
        before = verify.run_pytest(repro_rel, wt_path)
        if before.get("passed"):
            # A test that already passes proves nothing: the defect wasn't
            # reproduced, or (feature) the acceptance test asserts nothing real.
            note = ("acceptance test passed before building — it asserts nothing "
                    "real; not a valid spec" if is_feature
                    else "repro test passed on incumbent — not reproduced")
            status = "vacuous_acceptance_test" if is_feature else "not_reproducible"
            kanban.move(issue_id, "backlog", fix={"note": note},
                        attempts=int(issue.get("attempts", 0)) + 1)
            _teardown(wt_path)
            return {"status": status, "issue": issue_id}

    # incumbent suite baseline (pre-existing failures don't block; new ones do)
    baseline = verify.suite_failures(str(REPO_ROOT))

    # PHASE 2 — minimal fix / build the capability
    if is_feature:
        p2_head = "\nPHASE 2 OF 2 — BUILD THE CAPABILITY.\n"
        p2_body = (
            f"The acceptance test at {repro_rel} is committed and failing; make it pass by "
            f"actually IMPLEMENTING the capability in the real modules (never by weakening "
            f"the test). Touch at least one real implementation file — a test-only change "
            f"is rejected. Target: {issue.get('fix_target') or 'locate the right module from the spec'}. ")
    else:
        p2_head = ("\nPHASE 2 OF 2 — FIX MINIMALLY.\n" if not use_llm_replay else "\nFIX MINIMALLY.\n")
        p2_body = (
            f"The failing repro test at {repro_rel} is committed; make it pass by fixing the "
            f"REAL defect (never by weakening the test). " if not use_llm_replay else
            f"The defect shows in real episode outputs; fix the {issue.get('fix_target','prompt/config')} "
            f"so the behavior changes. ")
    p2 = (
        _issue_brief(issue) + p2_head + p2_body +
        "Smallest correct change; match surrounding style; run the relevant tests; "
        "then `git add -A && git commit`. Do NOT touch coaching/rubrics, coaching/checks.py, "
        "or evals/ — the measuring stick is out of bounds."
    )
    await _run_codebot(p2, wt_path)
    if _commits_since(wt_path, base_sha) < (1 if use_llm_replay else 2):
        kanban.move(issue_id, "backlog", fix={"note": "phase2 produced no fix commit"},
                    attempts=int(issue.get("attempts", 0)) + 1)
        _teardown(wt_path)
        return {"status": "fix_not_committed", "issue": issue_id}

    # AFTER evidence + gates — all Python-run
    if use_llm_replay:
        after = await verify.run_llm_cases(llm_cases, wt_path,
                                           agent=issue.get("agent", "conductor"))
        repro_ok = bool(after.get("all_passed"))
    else:
        after = verify.run_pytest(repro_rel, wt_path)
        repro_ok = bool(after.get("passed"))

    regression = verify.regression_gate(baseline, wt_path)
    changed = verify.diff_files(wt_path, base_sha)
    gaming = verify.anti_gaming(changed, issue)
    # a capability request must ship real implementation, not just a green test
    feat = verify.feature_gate(changed, issue) if is_feature else {"passed": True}

    review = await _fresh_review(wt_path, base_sha)

    gates = {"repro_after": repro_ok, "regression": regression["passed"],
             "anti_gaming": gaming["passed"], "feature_impl": feat["passed"]}
    if not all(gates.values()):
        kanban.move(issue_id, "backlog",
                    fix={"note": f"gates failed: {[k for k, v in gates.items() if not v]}",
                         "regression": regression, "gaming": gaming, "feature": feat},
                    attempts=int(issue.get("attempts", 0)) + 1)
        _teardown(wt_path, keep_branch=True)
        return {"status": "gates_failed", "issue": issue_id, "gates": gates}

    rc, sha = _git(wt_path, "rev-parse", "HEAD")
    rc2, diff_stat = _git(wt_path, "diff", "--stat", f"{base_sha}..HEAD")
    evidence = {
        "branch": branch, "commit": sha.strip()[:12], "files_changed": changed,
        "before": {k: v for k, v in before.items() if k != "per_case"} if use_llm_replay
                  else {"repro_failed_as_expected": True, "tail": str(before.get("tail",""))[-300:]},
        "after": {k: v for k, v in after.items() if k != "per_case"} if use_llm_replay
                 else {"repro_passes": True, "tail": str(after.get("tail",""))[-300:]},
        "regression": regression, "anti_gaming": gaming,
        "review": {"verdict": review.get("verdict"), "notes": review.get("notes", "")[:800]},
    }

    description = (f"Coach fix ready: {issue['title'][:90]} "
                   f"(RCA: {issue['rca_class']}, {issue.get('count',1)}x)")
    details = {
        "issue_id": issue_id, "branch": branch, "worktree": wt_path,
        "diff_stat": diff_stat[-800:], **evidence,
        # on approval the trusted executor merges the EXACT reviewed commit —
        # never the mutable branch ref (TOCTOU); git revert undoes it
        "command": f"git -C {REPO_ROOT} merge --no-ff --no-edit {sha.strip()}",
    }
    reasoning = (
        f"The coaching loop found this defect in {issue.get('count',1)} real runs of "
        f"'{issue['agent']}', classified it as {issue['rca_class']} ({issue.get('rca_why','')[:150]}), "
        f"reproduced it, fixed it, and verified: repro now passes, zero new test failures, "
        f"diff clean of eval/rubric tampering, fresh review: {review.get('verdict')}. "
        f"Holding for your approval — self-modifications are never auto-applied.")

    if dry_run:
        kanban.move(issue_id, "in_review", fix=evidence)
        return {"status": "would_propose", "issue": issue_id, "branch": branch,
                "evidence": evidence, "proposal": {"description": description,
                                                   "details_keys": sorted(details)}}

    result = await broker.propose(
        action_type="self_modify", description=description, details=details,
        reasoning=reasoning, project="sentinel-self",
        criticality_hint="critical", reversibility="reversible",
        best_effort_plan="hold — never auto-apply self-modifications",
        source="coach")
    kanban.move(issue_id, "in_review", fix={**evidence, "action_id": result.get("id", "")})
    return {"status": "proposed", "issue": issue_id, "branch": branch,
            "broker_result": result, "evidence": evidence}


async def _fresh_review(wt_path: str, base_sha: str) -> dict:
    """Fresh-context review of the diff. An empty review is review-FAILED
    (never silently clean — that exact bug is on the coach's own findings list)."""
    from brain.claude_runner import run_claude
    rc, diff = _git(wt_path, "diff", f"{base_sha}..HEAD")
    if rc != 0 or not diff.strip():
        return {"verdict": "no-diff", "notes": ""}
    prompt = ("Review this diff for correctness bugs, security issues, and test "
              "weakening (a test edited to pass without the defect being fixed). "
              "Terse, violations-only; if genuinely clean say 'LGTM'.\n\n"
              f"```diff\n{diff[:12000]}\n```")
    notes = ""
    try:
        async for ev in run_claude(prompt, agent="review-bot", tier="read",
                                   working_dir=wt_path, max_turns=0,
                                   episode_source="coach-run"):
            if ev.type == "result":
                notes = ev.content or ""
    except Exception as e:
        return {"verdict": "review-error", "notes": str(e)[:200]}
    if not notes.strip():
        return {"verdict": "review-failed-empty", "notes": ""}
    # LGTM may arrive mid-note ("Verified against the call sites. **LGTM** — …");
    # accept a standalone LGTM token unless it is negated.
    import re as _re
    head = notes[:600]
    clean = bool(_re.search(r"\bLGTM\b", head, _re.I)) and \
        not _re.search(r"\b(not|no|isn't|far from)\s+(\*\*)?LGTM\b", head, _re.I)
    return {"verdict": "clean" if clean else "concerns", "notes": notes[:1500]}


def _teardown(wt_path: str, keep_branch: bool = False) -> None:
    _git(REPO_ROOT, "worktree", "remove", "--force", wt_path)
    # branches are kept when a proposal references them; abandoned attempts
    # keep theirs only if explicitly asked (debugging)
    if not keep_branch:
        pass  # branch stays too — it's cheap and preserves forensic history


async def run_fix_cycle(broker, max_issues: int = 1, dry_run: bool = False) -> dict:
    """The heartbeat entrypoint: pick and fix up to max_issues backlog issues."""
    kanban.reconcile()   # fold in approvals/rejections since the last cycle first
    results = []
    for _ in range(max_issues):
        issue = kanban.pick_next()
        if not issue:
            break
        logger.info("fix cycle: picked %s (%s/%s)", issue["id"], issue["agent"], issue["severity"])
        try:
            results.append(await fix_one(issue, broker, dry_run=dry_run))
        except Exception as e:
            logger.warning("fix_one(%s) crashed: %s", issue["id"], e)
            kanban.move(issue["id"], "backlog", fix={"note": f"fixer crashed: {e}"},
                        attempts=int(issue.get("attempts", 0)) + 1)
            results.append({"status": "error", "issue": issue["id"], "error": str(e)[:300]})
    return {"fixed": results, "board": kanban.board_summary()}
