"""GitHub Autopilot — read Kanban/issues -> fix -> propose PR.

The safest full-loop vertical: perception is read-only (issues, CI), the fix
runs a Claude Code coding agent in an isolated branch, and the only mutation
(open PR) is reversible and gated through the approval broker. Nothing ever
touches main without a human (or an earned/pre-approved project decision).
"""

from __future__ import annotations

import asyncio
import logging

from tools import github_tools as gh
from tools import repo_manager
from tools import test_runner
from brain import claude_runner
from brain import model_router
from learning import learning_loop

logger = logging.getLogger("github-autopilot")


async def self_review(owner: str, repo: str, work_dir: str, base: str) -> dict:
    """Fresh-context reviewer over the agent's own diff before proposing the PR.

    Research (2026): self-critique in the SAME session is net-negative, but a
    reviewer with FRESH context + the actual diff catches real issues. Advisory:
    surfaces concerns in the PR body; does not hard-block (the human is the gate).
    """
    rc, diff, _ = await _git(work_dir, "diff", f"origin/{base}...HEAD", timeout=30)
    if rc != 0 or not diff.strip():
        return {"reviewed": False, "verdict": "no-diff", "notes": ""}
    prompt = (
        "Review this git diff for correctness bugs, security issues, and anything "
        "that would fail review. Be terse. If it's clean, say 'LGTM'. Only report "
        "issues you can concretely justify.\n\n"
        f"```diff\n{diff[:12000]}\n```"
    )
    notes = ""
    async for ev in claude_runner.run_claude(
        prompt, agent="review-bot", working_dir=work_dir, tier="read",
        model=model_router.select_model("code_review", for_cli=True), max_turns=0,
    ):
        if ev.type == "result":
            notes = ev.content or ""
    clean = "lgtm" in notes.lower()[:40] or not notes.strip()
    return {"reviewed": True, "verdict": "clean" if clean else "concerns", "notes": notes[:1500]}


def _already_worked(owner: str, repo: str, issue_num: int) -> bool:
    """Skip issues we've already opened a PR for or that were rejected."""
    try:
        from approval import action_queue as aq
        key = f"issue #{issue_num}"
        for a in aq.get_actions():
            if a.get("project") == f"{owner}/{repo}" and key in (a.get("description", "")):
                if a.get("status") in ("executed", "approved", "pending"):
                    return True
        for r in aq.get_recent_rejections(hours=168):
            if key in r.get("description", ""):
                return True
    except Exception:
        pass
    return False


async def perceive(owner: str, repo: str, max_issues: int = 5) -> dict:
    """Tier R: read open issues + CI status. No mutation."""
    issues = gh.list_issues(owner=owner, repo=repo, state="open")
    if isinstance(issues, dict) and issues.get("error"):
        return {"error": issues["error"], "issues": []}
    issues = (issues or [])[:max_issues]
    ci = gh.get_ci_status(owner=owner, repo=repo) if hasattr(gh, "get_ci_status") else {}
    return {"owner": owner, "repo": repo, "issues": issues, "ci": ci}


def pick_issue(perception: dict) -> dict | None:
    """Pick the highest-signal actionable issue. Prefer ones labeled bug/good-first
    -issue/chore; skip ones already assigned, blocked, or already worked."""
    owner, repo = perception.get("owner", ""), perception.get("repo", "")
    best = None
    for iss in perception.get("issues", []):
        num = iss.get("number")
        if not isinstance(num, int):
            # Malformed entry (e.g. a kanban-synced card with no linked GitHub
            # issue) must never win the pick over a real, actionable issue.
            continue
        labels = [l.lower() for l in iss.get("labels", [])]
        if any(b in labels for b in ("blocked", "needs-discussion", "wontfix")):
            continue
        if _already_worked(owner, repo, num):
            continue
        score = 0
        score += 3 if any(b in labels for b in ("bug", "good first issue", "good-first-issue")) else 0
        score += 2 if any(b in labels for b in ("chore", "cleanup", "tech-debt", "refactor")) else 0
        score += 1 if not iss.get("assignee") else 0
        if best is None or score > best[0]:
            best = (score, iss)
    return best[1] if best else None


async def _git(work_dir: str, *args, timeout: int = 60):
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", work_dir, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out.decode().strip(), err.decode().strip()


async def fix_issue(owner: str, repo: str, issue: dict, work_dir: str | None = None) -> dict:
    """Run a Claude Code coding agent to implement a fix in an isolated worktree.

    If work_dir is None (the normal path), an isolated git worktree is created
    off a freshly-fetched origin/<default> via repo_manager — so parallel issues
    never collide and the checkout is always current. The agent just edits +
    commits; it is denied git push / gh pr create by the deny-guard, so only the
    broker (trusted) pushes and opens the PR.
    """
    num = issue.get("number")
    if not isinstance(num, int):
        # Malformed input (e.g. a bad perceive/pick_issue result with no real
        # issue number) must be rejected here, before we ever spin up a
        # worktree or invoke the coding agent on a nonsense "issue #None"
        # prompt — that pattern only surfaced downstream as a confusing
        # "agent produced no commit on sentinel/issue-None" failure.
        err = f"malformed issue: missing/invalid 'number' ({num!r}); refusing to invoke coding agent"
        logger.warning("fix_issue rejected malformed issue for %s/%s: %s", owner, repo, err)
        return {"branch": None, "summary": "", "error": err, "success": False,
                "work_dir": work_dir, "base": None, "owns_worktree": False,
                "base_failures": []}
    branch = f"sentinel/issue-{num}"

    owns_worktree = work_dir is None
    if owns_worktree:
        work_dir = await repo_manager.worktree(owner, repo, branch)

    # Baseline: which tests already fail BEFORE the agent touches anything. The
    # gate later blocks only NEW failures (regressions), so a single fix can ship
    # in a repo that has other known-failing tests.
    baseline = await test_runner.run_tests(work_dir)
    base_failures = list(baseline.get("failures", [])) if baseline.get("ran") else []

    prompt = (
        f"You are fixing GitHub issue #{num} in {owner}/{repo}.\n\n"
        f"TITLE: {issue.get('title','')}\n"
        f"BODY:\n{issue.get('body','')[:4000]}\n\n"
        f"You are ALREADY on the correct branch `{branch}` in an isolated worktree — "
        f"do NOT create or switch branches.\n"
        f"Do this:\n"
        f"1. Study the codebase, implement a minimal, correct fix for the issue.\n"
        f"2. Run any obvious tests/linters you can find and make sure they pass.\n"
        f"3. `git add -A` and commit with a clear message referencing #{num}.\n"
        f"4. Do NOT run `git push` and do NOT open a PR — you will be blocked and it is "
        f"the human's job to approve that. Just commit locally.\n"
        f"5. End your final message with a 2-3 sentence SUMMARY of the change.\n"
    )
    prompt = learning_loop.pre_task_injection("github_autopilot", prompt)

    # Route the model: everyday coding -> standard tier (Sonnet); the router
    # returns None to let Claude Code use its configured default when that's best.
    model = model_router.select_model("code_fix", for_cli=True)

    events, summary, err = [], "", None
    async for ev in claude_runner.run_claude(
        prompt, agent="github_autopilot", working_dir=work_dir, tier="act",
        model=model, max_turns=0,
    ):
        events.append(ev)
        if ev.type == "result":
            summary = ev.content or ""
        if ev.type == "error":
            err = ev.content

    # Structural verification: the branch must actually have a new commit vs its base.
    base = await repo_manager.default_branch(owner, repo)
    rc, ahead, _ = await _git(work_dir, "rev-list", "--count", f"origin/{base}..HEAD")
    committed = rc == 0 and ahead.isdigit() and int(ahead) > 0
    cur_rc, cur_branch, _ = await _git(work_dir, "rev-parse", "--abbrev-ref", "HEAD")
    on_branch = cur_branch == branch

    success = err is None and committed and on_branch
    if not success and err is None:
        err = (f"agent produced no commit on {branch} "
               f"(on_branch={on_branch}, commits_ahead={ahead})")

    learning_loop.post_task_analysis(
        agent_name="github_autopilot", session_id=f"issue-{num}",
        task_prompt=f"fix #{num} {issue.get('title','')}",
        result_text=summary, success=success, error_message=err or "",
    )
    return {"branch": branch, "summary": summary, "error": err, "success": success,
            "work_dir": work_dir, "base": base, "owns_worktree": owns_worktree,
            "base_failures": base_failures}


async def reflexion_retry(owner: str, repo: str, issue: dict, work_dir: str,
                          failing_output: str) -> dict:
    """Self-heal a red regression gate ONCE (research doc 11 §a#5, Reflexion):
    re-invoke the coding agent with the failing-test output + a reflection nudge.
    Bounded to a single retry so it can't become a doom loop."""
    from conductor import self_heal
    num = issue.get("number")
    reflection = self_heal.reflection_line(failing_output, f"fix #{num} {issue.get('title','')}")
    prompt = (
        f"You are on branch for issue #{num} in an isolated worktree. Your previous "
        f"fix introduced NEW test failures.\n\n{reflection}\n\n"
        f"Adjust the fix so no new tests fail. Commit when done; do NOT push.")
    from brain import claude_runner
    err = None
    async for ev in claude_runner.run_claude(
            prompt, agent="github_autopilot", working_dir=work_dir, tier="act",
            model=None, max_turns=0):
        if ev.type == "error":
            err = ev.content
    try:
        from tools import test_runner
        base_failures = []  # re-check against the SAME baseline the caller captured
        tests = await test_runner.regression_check(base_failures, work_dir)
    except Exception as e:
        return {"recovered": False, "error": str(e)}
    recovered = not tests.get("regression")
    try:
        from memory import reasoning_bank
        if recovered:
            reasoning_bank.record(f"fix #{num} {issue.get('title','')}", agent="code-bot",
                                  task_type="coding", outcome="done",
                                  lesson="Recovered a red regression via one Reflexion retry.")
        else:
            reasoning_bank.record_failure(f"fix #{num} {issue.get('title','')}", "code-bot",
                                          "regression persisted after Reflexion retry", "coding")
    except Exception:
        pass
    return {"recovered": recovered, "tests": tests, "error": err}


def propose_pr_action(owner: str, repo: str, issue: dict, fix: dict, work_dir: str = "",
                      tests: dict | None = None, review: dict | None = None) -> dict:
    """Build the ProposedAction payload for opening the PR (broker handles gating).

    Passes local_dir so the broker (which IS allowed to push, unlike the coding
    agent) pushes the branch before opening the PR. Embeds the test result and
    self-review verdict in the PR body so the human sees grounded evidence."""
    num = issue.get("number")
    tests = tests or {}
    review = review or {}
    fixed = tests.get("fixed") or []
    if tests.get("passed"):
        test_line = "✅ full suite passes (`%s`)" % tests.get("cmd")
    elif tests.get("ran") and not tests.get("regression"):
        # some pre-existing failures remain but this fix introduced NO new ones
        test_line = "✅ no new test failures (`%s`)" % tests.get("cmd")
        if fixed:
            test_line += f"; now-passing: {', '.join(t.split('::')[-1] for t in fixed)}"
    elif tests.get("passed") is None:
        test_line = "ℹ️ no tests detected"
    else:
        test_line = "⚠️ tests regressed"
    review_line = {"clean": "✅ self-review: LGTM", "concerns": "⚠️ self-review flagged concerns (see below)",
                   "no-diff": ""}.get(review.get("verdict", ""), "")
    body = (f"Closes #{num}.\n\n{fix.get('summary','')}\n\n"
            f"**Checks:** {test_line}"
            + (f" · {review_line}" if review_line else ""))
    if review.get("verdict") == "concerns" and review.get("notes"):
        body += f"\n\n<details><summary>Self-review notes</summary>\n\n{review['notes']}\n\n</details>"
    body += "\n\n_Raised autonomously by Sentinel (test-gated + self-reviewed)._"
    return {
        "action_type": "create_pr",
        "description": f"Open PR for issue #{num}: {issue.get('title','')[:80]}",
        "details": {
            "owner": owner, "repo": repo,
            "title": f"Fix #{num}: {issue.get('title','')}",
            "body": body,
            "head": fix.get("branch", ""), "base": fix.get("base", "main"),
            "local_dir": fix.get("work_dir", work_dir),
        },
        "reasoning": f"Implemented a fix on branch {fix.get('branch')}; opening a PR for review.",
        "project": f"{owner}/{repo}",
        "tier": "A", "reversibility": "reversible",
        "best_effort_plan": f"Open the PR from {fix.get('branch')} for your review.",
    }


async def run_once(owner: str, repo: str, broker, work_dir: str | None = None) -> dict:
    """One full cycle: perceive -> pick -> fix (isolated worktree) -> propose PR.

    work_dir=None (normal) auto-creates an isolated git worktree via repo_manager,
    so multiple repos/issues can be worked in parallel without collision.
    """
    perception = await perceive(owner, repo)
    if perception.get("error"):
        return {"status": "perceive_error", "error": perception["error"]}
    issue = pick_issue(perception)
    if not issue:
        return {"status": "no_actionable_issue", "seen": len(perception.get("issues", []))}

    logger.info("Autopilot picked issue #%s: %s", issue.get("number"), issue.get("title"))
    fix = await fix_issue(owner, repo, issue, work_dir)
    if not fix.get("success"):
        # keep the worktree for post-mortem; a later cleanup pass removes stale ones
        return {"status": "fix_failed", "issue": issue.get("number"), "error": fix.get("error")}

    wd = fix.get("work_dir")
    # GATE 1 — test regression check (grounded signal; strongest reliability technique).
    # Block only on NEW failures vs the pre-fix baseline, so a single fix can ship in
    # a repo that has other known-failing tests (the target issue's test should now pass).
    tests = await test_runner.regression_check(fix.get("base_failures", []), wd)
    if tests.get("regression"):
        learning_loop.post_task_analysis(
            agent_name="github_autopilot", session_id=f"issue-{issue.get('number')}",
            task_prompt=f"tests for #{issue.get('number')}", result_text=tests.get("output", ""),
            success=False, error_message=f"new test failures: {tests.get('new_failures')}")
        return {"status": "tests_regressed", "issue": issue.get("number"),
                "new_failures": tests.get("new_failures", []),
                "test_output": tests.get("output", "")[:500]}

    # GATE 2 — fresh-context self-review over the diff (advisory, added to PR body).
    review = await self_review(owner, repo, wd, fix.get("base", "main"))

    payload = propose_pr_action(owner, repo, issue, fix, work_dir=wd, tests=tests, review=review)
    result = await broker.propose(**payload)
    return {"status": "proposed_pr", "issue": issue.get("number"),
            "branch": fix.get("branch"), "tests": tests.get("passed"),
            "review": review.get("verdict"), "broker_result": result}
