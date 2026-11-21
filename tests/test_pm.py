"""Unit tests for the PM autopilot — the decision logic, no LLM, no real git."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coaching import pm
from approval import action_queue as aq


@pytest.fixture(autouse=True)
def tmp_kv(tmp_path, monkeypatch):
    """Isolate kv state (pm config, restart flag) per test."""
    import conductor.kv as kv
    monkeypatch.setattr(kv, "STATE_DIR", tmp_path)
    yield


def _card(age_hours=0.0, pm_review=None, status="pending"):
    return {
        "id": "action-pm1", "type": "self_modify", "source": "coach",
        "status": status, "criticality": "critical",
        "description": "Coach fix ready: test", "reasoning": "r",
        "proposed_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "details": {"branch": "sentinel/coach/ci-x", "commit": "abc123def",
                    "command": "git merge", "issue_id": "ci-x",
                    "before": {"repro_failed_as_expected": True},
                    "after": {"repro_passes": True},
                    "regression": {"passed": True}, "review": {"verdict": "clean"}},
        **({"pm_review": pm_review} if pm_review else {}),
    }


class _Router:
    def __init__(self):
        self.sent = []
    async def broadcast(self, text):
        self.sent.append(text)


# ─────────────────────────── eligibility + gates ───────────────────────────

def test_only_coach_self_modify_is_eligible():
    ok, _ = pm.eligible(_card())
    assert ok is True
    ok, _ = pm.eligible({**_card(), "source": "evolve"})
    assert ok is False
    ok, _ = pm.eligible({**_card(), "type": "create_pr"})
    assert ok is False
    thin = _card(); thin["details"] = {"branch": "b"}
    ok, why = pm.eligible(thin)
    assert ok is False and "evidence" in why


def _fake_git(responses):
    def fake(*args, cwd=None, timeout=60):
        key = args[0] if args else ""
        if key == "rev-parse":
            return 0, responses.get("tip", "abc123def456")
        if key == "merge-base":
            return 0, "base000"
        if key == "diff" and "--name-only" in args:
            return 0, "\n".join(responses.get("files", []))
        if key == "diff" and "--shortstat" in args:
            return 0, responses.get("stat", " 2 files changed, 30 insertions(+), 2 deletions(-)")
        if key == "worktree":
            return 0, ""
        if key == "merge-tree":
            return responses.get("merge_rc", 0), ""
        if key == "merge":
            return 0, ""
        return 0, ""
    return fake


def test_gates_pass_on_clean_scoped_diff(monkeypatch):
    monkeypatch.setattr(pm, "_git", _fake_git({
        "files": ["verticals/github_autopilot.py", "tests/coaching_repro/test_ci_x.py"]}))
    monkeypatch.setattr("coaching.verify.run_pytest",
                        lambda target, cwd, timeout=600: {"passed": True, "tail": ""})
    gates = pm.deterministic_gates(_card())
    assert gates["passed"] is True, gates["reasons"]


def test_gates_block_files_outside_whitelist(monkeypatch):
    # legacy/standard policy: full mode (the 2026-08-15 default) deliberately
    # allows brain/ + conductor/ — its gate expectations live in
    # tests/test_self_repair.py::test_pm_gate_paths_never_self_merge
    monkeypatch.setenv("SENTINEL_SELF_REPAIR", "standard")
    monkeypatch.setattr("coaching.verify.run_pytest",
                        lambda target, cwd, timeout=600: {"passed": True, "tail": ""})
    for bad in ("approval/broker.py", "config/paths.py", "brain/claude_runner.py",
                "conductor/daemon.py", ".claude/hooks/deny-guard.sh",
                ".claude/agents/pm.md", "memory/gateway.py", "tests/test_pm.py"):
        monkeypatch.setattr(pm, "_git", _fake_git({
            "files": [bad, "tests/coaching_repro/test_ci_x.py"]}))
        gates = pm.deterministic_gates(_card())
        assert gates["passed"] is False, f"{bad} slipped through the whitelist"
        assert any("whitelist" in r for r in gates["reasons"])


def test_gates_block_merge_conflicts_with_head(monkeypatch):
    monkeypatch.setattr(pm, "_git", _fake_git({
        "files": ["verticals/x.py", "tests/coaching_repro/test_ci_x.py"],
        "merge_rc": 1}))
    monkeypatch.setattr("coaching.verify.run_pytest",
                        lambda target, cwd, timeout=600: {"passed": True, "tail": ""})
    gates = pm.deterministic_gates(_card())
    assert gates["passed"] is False
    assert any("conflict" in r for r in gates["reasons"])


def test_gates_block_when_branch_moved_since_evidence(monkeypatch):
    monkeypatch.setattr(pm, "_git", _fake_git({
        "tip": "fffffff000", "files": ["verticals/x.py"]}))
    gates = pm.deterministic_gates(_card())
    assert gates["passed"] is False
    assert any("moved since evidence" in r for r in gates["reasons"])


def test_gates_block_oversized_diffs(monkeypatch):
    monkeypatch.setattr(pm, "_git", _fake_git({
        "files": [f"mod_{i}.py" for i in range(12)],
        "stat": " 12 files changed, 900 insertions(+)"}))
    gates = pm.deterministic_gates(_card())
    assert gates["passed"] is False
    assert any("cap" in r for r in gates["reasons"])


def test_gates_block_failing_repro_at_tip(monkeypatch):
    monkeypatch.setattr(pm, "_git", _fake_git({
        "files": ["verticals/x.py", "tests/coaching_repro/test_ci_x.py"]}))
    monkeypatch.setattr("coaching.verify.run_pytest",
                        lambda target, cwd, timeout=600: {"passed": False, "tail": "1 failed"})
    gates = pm.deterministic_gates(_card())
    assert gates["passed"] is False
    assert any("FAILS at branch tip" in r for r in gates["reasons"])


# ─────────────────────────── sweep decision logic ───────────────────────────

def test_sweep_reviews_new_card_and_announces(monkeypatch):
    card = _card()
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    stored = {}
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: stored.update(f))
    monkeypatch.setattr(pm, "deterministic_gates",
                        lambda a: {"passed": True, "reasons": [], "files": ["x.py"]})
    async def fake_verdict(a, files):
        return {"decision": "approve", "reasons": ["scoped, reversible"], "raw": ""}
    monkeypatch.setattr(pm, "_pm_agent_verdict", fake_verdict)

    router = _Router()
    out = asyncio.run(pm.sweep(router))
    assert out["reviewed"] == ["action-pm1"]
    assert stored["pm_review"]["decision"] == "approve"
    assert any("Auto-merges" in s for s in router.sent)


def _reviewed_hours_ago(h):
    return {"decision": "approve", "reasons": ["ok"],
            "at": (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()}


def test_sweep_auto_approves_after_quiet_window(monkeypatch):
    card = _card(age_hours=9, pm_review=_reviewed_hours_ago(4))
    approved = []
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: None)
    monkeypatch.setattr(aq, "approve_action", lambda aid, resolved_by="": (
        approved.append((aid, resolved_by)) or
        {"execution_result": {"exit_code": 0, "stdout": "Merge made"}}))
    monkeypatch.setattr(pm, "deterministic_gates",
                        lambda a: {"passed": True, "reasons": [], "files": ["x.py"],
                                   "tip": "abc123def456"})
    import conductor.kv as kv

    router = _Router()
    out = asyncio.run(pm.sweep(router))
    assert approved == [("action-pm1", "pm-autopilot")]
    assert out["auto_approved"] == ["action-pm1"]
    assert kv.get("restart_required")           # restart scheduled
    assert kv.get("pm_daily", {}).get("n") == 1  # durable cap counter
    assert any("auto-approved" in s for s in router.sent)


def test_window_anchors_to_notification_not_card_age(monkeypatch):
    """A stale card (proposed days ago, daemon was down) must still give
    the owner the FULL window from when he was actually notified."""
    card = _card(age_hours=72, pm_review=_reviewed_hours_ago(0.5))
    approved = []
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    asyncio.run(pm.sweep(None))
    assert approved == []


def test_chat_activity_restarts_the_silence_window(monkeypatch):
    """Any message from the owner pauses the countdown — prose objections the
    parser can't resolve must still hold the merge."""
    card = _card(age_hours=9, pm_review=_reviewed_hours_ago(5))
    approved = []
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    monkeypatch.setattr(pm, "_last_chat_activity_hours_ago", lambda: 0.2)
    asyncio.run(pm.sweep(None))
    assert approved == []


def test_sweep_never_auto_approves_before_window_or_on_hold(monkeypatch):
    approved = []
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: None)
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    # too young
    young = _card(age_hours=1, pm_review={"decision": "approve", "reasons": []})
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [young])
    asyncio.run(pm.sweep(None))
    # PM said hold
    held = _card(age_hours=9, pm_review={**_reviewed_hours_ago(5), "decision": "hold"})
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [held])
    asyncio.run(pm.sweep(None))
    assert approved == []


def test_sweep_reverifies_gates_at_decision_time(monkeypatch):
    """A PM 'approve' from 3h ago must not merge if the branch changed since."""
    card = _card(age_hours=9, pm_review=_reviewed_hours_ago(4))
    approved, stored = [], {}
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: stored.update(f))
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    monkeypatch.setattr(pm, "deterministic_gates",
                        lambda a: {"passed": False,
                                   "reasons": ["branch moved since evidence"], "files": []})
    out = asyncio.run(pm.sweep(None))
    assert approved == []
    assert stored["pm_review"]["decision"] == "hold"


def test_sweep_respects_daily_cap(monkeypatch):
    monkeypatch.setenv("SENTINEL_SELF_REPAIR", "standard")   # cap=3 policy
    card = _card(age_hours=9, pm_review=_reviewed_hours_ago(4))
    approved = []
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: None)
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    monkeypatch.setattr(pm, "_auto_approved_today", lambda: 3)
    out = asyncio.run(pm.sweep(None))
    assert approved == []
    assert out["held"] and "cap" in out["held"][0][1]


def test_disagreement_disables_autopilot(monkeypatch):
    rejected = _card(status="rejected",
                     pm_review={"decision": "approve", "reasons": ["ok"]})
    def get_actions(status=""):
        return [rejected] if status == "rejected" else []
    monkeypatch.setattr(aq, "get_actions", get_actions)
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [])
    monkeypatch.setattr(aq, "update_action", lambda aid, **f: None)

    assert pm.config()["enabled"] is True
    asyncio.run(pm.sweep(None))
    cfg = pm.config()
    assert cfg["enabled"] is False
    assert "rejected" in cfg["disabled_reason"]


def test_disabled_autopilot_still_reviews_but_never_merges(monkeypatch):
    pm.disable("test")
    card = _card(age_hours=9, pm_review=_reviewed_hours_ago(5))
    approved = []
    monkeypatch.setattr(aq, "get_pending_actions", lambda: [card])
    monkeypatch.setattr(aq, "get_actions", lambda status="": [])
    monkeypatch.setattr(aq, "approve_action",
                        lambda aid, resolved_by="": approved.append(aid))
    asyncio.run(pm.sweep(None))
    assert approved == []
