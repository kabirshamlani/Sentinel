"""Unit tests for the coaching package — deterministic parts only (no LLM)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coaching import checks, kanban, rca, verify
from coaching.episodes import strip_injection
from coaching.rubrics import RubricError, load_rubrics


# ─────────────────────────── rubrics ───────────────────────────

def test_all_shipped_rubrics_load_and_validate():
    rubrics = load_rubrics()
    assert len(rubrics) >= 12
    for name in ("code-bot", "review-bot", "comms-drafter", "agent-chat", "meeting_intel"):
        assert name in rubrics
    # alias resolution
    from coaching.rubrics import rubric_for
    assert rubric_for("github_autopilot", rubrics)["agent"] == "code-bot"
    assert rubric_for("inbox_triage", rubrics)["agent"] == "comms-drafter"
    assert rubric_for("no-such-agent", rubrics) is None


def test_bad_rubric_fails_loudly(tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "agent: x\nversion: 1\nrole: r\ndefinition_of_good: d\n"
        "episode_checks:\n  - check: not_a_real_check\n", encoding="utf-8")
    with pytest.raises(RubricError):
        load_rubrics(tmp_path)


# ─────────────────────────── checks ───────────────────────────

def _ep(**kw):
    base = {"id": "ep-test", "agent": "x", "input": "", "output": "",
            "success": None, "error": "", "fidelity": "full", "meta": {}, "source": "test"}
    base.update(kw)
    return base


def test_success_label_consistent_catches_mislabels():
    ok, ev = checks.success_label_consistent(
        _ep(success=True, output="There's no task in your message."), {})
    assert ok is False and "no task" in ev
    ok, _ = checks.success_label_consistent(
        _ep(success=True, output="Deployed the fix, tests pass, PR body drafted."), {})
    assert ok is True
    # not applicable when the run wasn't labeled a success
    ok, _ = checks.success_label_consistent(_ep(success=False, output="x"), {})
    assert ok is None


def test_none_in_target_catches_null_flow():
    ok, ev = checks.none_in_target(_ep(input="fix #None "), {})
    assert ok is False
    ok, _ = checks.none_in_target(_ep(input="fix #42: crash on login"), {})
    assert ok is True


def test_commit_evidence_reads_structural_signals():
    ok, _ = checks.commit_evidence(_ep(error="agent produced no commit on sentinel/issue-3 (commits_ahead=0)"), {})
    assert ok is False
    ok, _ = checks.commit_evidence(_ep(meta={"status": "proposed_pr"}), {})
    assert ok is True
    ok, _ = checks.commit_evidence(_ep(), {})
    assert ok is None


def test_draft_nonempty_or_skip():
    ok, _ = checks.draft_nonempty_or_skip(
        _ep(source="action_queue", output="", meta={"action_type": "draft_gchat"}), {})
    assert ok is False
    ok, _ = checks.draft_nonempty_or_skip(
        _ep(source="action_queue", output="SKIP", meta={"action_type": "draft_gchat"}), {})
    assert ok is True
    ok, _ = checks.draft_nonempty_or_skip(
        _ep(source="work_log", output="", meta={}), {})
    assert ok is None


def test_answer_not_promise():
    ok, _ = checks.answer_not_promise(_ep(output="Good question — I'll look into it now."), {})
    assert ok is False
    ok, _ = checks.answer_not_promise(_ep(output="Done: 3 files updated, tests green."), {})
    assert ok is True


def test_no_doom_loop():
    looping = "\n".join(["calling tool read_email with same args"] * 4)
    ok, _ = checks.no_doom_loop(_ep(output=looping), {})
    assert ok is False


def test_strip_injection_recovers_clean_ask():
    polluted = "## Learned Rules (auto-generated)\n- rule\n\n---\n\nfix the login crash"
    assert strip_injection(polluted) == "fix the login crash"
    assert strip_injection("## Learned Rules only, truncated…") == ""
    assert strip_injection("plain ask") == "plain ask"


# ─────────────────────────── rca ───────────────────────────

def _verdict(agent="code-bot", check="none_in_target", ok=False, sev="high", eid="e1"):
    return {"ts": "2026-07-28T10:00:00+00:00", "episode_id": eid, "episode_ts": "",
            "agent": agent, "rubric_version": 1, "check": check, "verifier": "deterministic",
            "ok": ok, "severity": sev, "evidence": "literal None in task target", "stream": "t"}


def test_rca_clusters_and_respects_min_occurrences():
    rubrics = {"code-bot": {"agent": "code-bot", "min_occurrences": 2}}
    # one failure -> transient, two -> issue
    issues, transients = rca.build_issues([_verdict(eid="e1")], [], rubrics)
    assert not issues and len(transients) == 1
    issues, transients = rca.build_issues(
        [_verdict(eid="e1"), _verdict(eid="e2")], [], rubrics)
    assert len(issues) == 1 and not transients
    issue = issues[0]
    assert issue["rca_class"] == "tool-defect"
    assert issue["count"] == 2
    assert issue["rca_class"] in rca.CLASSES


def test_rca_static_findings_always_file():
    finding = {"check": "slug_map_covers_agent", "agent": "meeting_intel",
               "summary": "verticals invoke agent='meeting_intel' but no slug/agent file",
               "evidence": ["verticals/meeting_intel.py:55"], "severity": "high"}
    issues, _ = rca.build_issues([], [finding], {})
    assert len(issues) == 1
    assert issues[0]["rca_class"] == "tool-defect"
    assert issues[0]["severity"] == "high"


def test_rca_class_table_is_valid():
    for check, (cls, channel, _t, _w) in rca._CLASS_RULES.items():
        assert cls in rca.CLASSES, check
        assert channel in ("code", "prompt", "rubric", "instrumentation"), check


# ─────────────────────────── kanban ───────────────────────────

@pytest.fixture()
def tmp_kanban(tmp_path, monkeypatch):
    monkeypatch.setattr(kanban, "KANBAN_PATH", tmp_path / "kanban.json")
    monkeypatch.setattr(kanban, "COACH_DIR", tmp_path)
    # exercise the JSON fallback deterministically (beads has its own test)
    monkeypatch.setattr(kanban, "BEADS_DIR", tmp_path / "no-beads")
    return tmp_path


def _issue(iid="ci-1", fp="code-bot:none_in_target", sev="high", count=3):
    return {"id": iid, "fingerprint": fp, "title": "t", "agent": "code-bot",
            "check": "none_in_target", "rca_class": "tool-defect",
            "fix_channel": "code", "fix_target": "", "rca_why": "",
            "severity": sev, "count": count, "episode_ids": [], "evidence": [],
            "judge_notes": [], "created": datetime.now(timezone.utc).isoformat()}


def test_kanban_files_dedups_and_picks_by_severity(tmp_kanban):
    assert kanban.file_issue(_issue())["status"] == "filed"
    assert kanban.file_issue(_issue())["status"] == "duplicate"
    kanban.file_issue(_issue(iid="ci-2", fp="chat:promise", sev="medium", count=9))
    top = kanban.pick_next()
    assert top["id"] == "ci-1"          # high beats medium regardless of count
    assert kanban.move("ci-1", "in_review", fix={"branch": "b"})
    assert kanban.get("ci-1")["fix"]["branch"] == "b"
    assert kanban.pick_next()["id"] == "ci-2"
    board = kanban.board_summary()
    assert board == {"in_review": 1, "backlog": 1}


def test_kanban_rejected_recently_blocks_refile(tmp_kanban):
    kanban.file_issue(_issue())
    kanban.move("ci-1", "rejected")
    assert kanban.file_issue(_issue())["status"] == "duplicate"


def test_kanban_dedups_phantom_regression_of_fixed_wiring_issue(tmp_kanban, monkeypatch):
    """A wiring-check issue whose defect is already fixed must DEDUP, not re-file
    as a regression — even with no episode timestamps. Root cause of the
    ci-03096f42-r1 loop: the old guard required episode_ts on every evidence
    entry, which wiring findings never have."""
    import coaching.checks as checks
    wiring_issue = _issue(iid="ci-wire", fp="self_improve:journal_field_mismatch")
    wiring_issue["check"] = "journal_field_mismatch"
    wiring_issue["evidence"] = [{"evidence": "learning/self_improve.py:45", "stream": "wiring"}]
    kanban.file_issue(wiring_issue)
    kanban.move("ci-wire", "done")
    # the check no longer fires (defect fixed) -> a refile must dedup, not regress
    monkeypatch.setitem(checks.WIRING_CHECKS, "journal_field_mismatch", lambda params: [])
    res = kanban.file_issue(dict(wiring_issue, id="ci-wire"))
    assert res["status"] == "duplicate", f"phantom regression re-filed: {res}"
    # but if the check STILL fires (real regression), it re-files
    monkeypatch.setitem(checks.WIRING_CHECKS, "journal_field_mismatch",
                        lambda params: [{"check": "journal_field_mismatch", "summary": "back",
                                         "evidence": ["x"], "severity": "high"}])
    res = kanban.file_issue(dict(wiring_issue, id="ci-wire"))
    assert res["status"] == "filed", f"real regression blocked: {res}"


def test_kanban_done_allows_regression_refile_with_unique_id(tmp_kanban):
    kanban.file_issue(_issue())
    kanban.move("ci-1", "done")
    res = kanban.file_issue(_issue(iid="ci-1"))
    assert res["status"] == "filed"
    new_id = res["id"]
    assert new_id != "ci-1"                       # duplicate ids would corrupt move()/get()
    assert kanban.get(new_id)["regression_of"] == "ci-1"
    assert kanban.get("ci-1")["status"] == "done"  # the resolved row is untouched


def test_kanban_attempt_cap_prevents_livelock(tmp_kanban):
    kanban.file_issue(_issue())
    assert kanban.pick_next()["id"] == "ci-1"
    kanban.move("ci-1", "backlog", attempts=kanban.MAX_FIX_ATTEMPTS)
    assert kanban.pick_next() is None             # exhausted issues are never re-picked
    assert kanban.get("ci-1")["status"] == "backlog"  # but stay visible on the board


def test_kanban_reconcile_moves_merged_to_done(tmp_kanban, monkeypatch):
    import subprocess as sp
    kanban.file_issue(_issue())
    kanban.move("ci-1", "in_review", fix={"action_id": "action-x", "commit": "abc123"})

    class FakeDone:
        returncode = 0
    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeDone())
    monkeypatch.setattr("approval.action_queue.get_action",
                        lambda aid: {"status": "executed"}, raising=False)
    moved = kanban.reconcile()
    assert moved["done"] == ["ci-1"]
    assert kanban.get("ci-1")["status"] == "done"
    assert "ancestor" in kanban.get("ci-1")["fix"]["merged_evidence"]


def test_kanban_reconcile_requires_merge_evidence_not_flag(tmp_kanban, monkeypatch):
    kanban.file_issue(_issue())
    kanban.move("ci-1", "in_review", fix={"action_id": "action-x", "commit": "abc123"})

    class FakeNotAncestor:
        returncode = 1
    monkeypatch.setattr("subprocess.run", lambda *a, **k: FakeNotAncestor())
    # even an 'executed' action does NOT close the issue without the git evidence
    monkeypatch.setattr("approval.action_queue.get_action",
                        lambda aid: {"status": "executed"}, raising=False)
    moved = kanban.reconcile()
    assert moved["pending"] == ["ci-1"]
    assert kanban.get("ci-1")["status"] == "in_review"


# ─────────────────────────── verify ───────────────────────────

def test_anti_gaming_blocks_measure_edits_allows_own_test():
    issue = {"id": "ci-abc12345", "rca_class": "tool-defect"}
    res = verify.anti_gaming(
        ["verticals/github_autopilot.py", "tests/coaching_repro/test_ci_abc12345.py"], issue)
    # own repro test is allowed; note the {issue_id} pattern guards OTHER issues' tests
    assert res["passed"] is True or "tests/coaching_repro" not in str(res["violations"])
    res = verify.anti_gaming(["coaching/rubrics/code-bot.yaml"], issue)
    assert res["passed"] is False
    res = verify.anti_gaming(["evals/eval.json"], issue)
    assert res["passed"] is False
    # spec-wrong may touch the measure (human still reviews the diff)
    res = verify.anti_gaming(["coaching/rubrics/code-bot.yaml"],
                             {"id": "ci-x", "rca_class": "spec-wrong"})
    assert res["passed"] is True


def test_regression_gate_fails_when_suite_cannot_run(monkeypatch):
    # 'could not measure' must never read as 'zero failures'
    monkeypatch.setattr(verify, "suite_failures", lambda cwd, timeout=900: None)
    gate = verify.regression_gate({"tests/test_a.py::t"}, "/tmp")
    assert gate["passed"] is False and "cannot prove" in gate["error"]
    # and a None baseline equally fails
    monkeypatch.setattr(verify, "suite_failures", lambda cwd, timeout=900: set())
    gate = verify.regression_gate(None, "/tmp")
    assert gate["passed"] is False


def test_regression_gate_only_blocks_new_failures(monkeypatch):
    base = {"tests/test_a.py::test_old_break"}
    monkeypatch.setattr(verify, "suite_failures",
                        lambda cwd, timeout=900: {"tests/test_a.py::test_old_break"})
    gate = verify.regression_gate(base, "/tmp")
    assert gate["passed"] is True and gate["pre_existing"]
    monkeypatch.setattr(verify, "suite_failures",
                        lambda cwd, timeout=900: base | {"tests/test_b.py::test_new"})
    gate = verify.regression_gate(base, "/tmp")
    assert gate["passed"] is False and gate["new_failures"] == ["tests/test_b.py::test_new"]


def test_not_regex_assertion_and_multiline_promise_replay():
    from learning.evolve import check_assertion
    promise_assert = verify.make_llm_cases(
        {"id": "i", "check": "answer_not_promise", "agent": "agent-chat",
         "episode_ids": ["e1"]},
        {"e1": {"id": "e1", "input": "do the thing"}})
    # derive the assertion directly from the map to test the shared grammar
    a = {"type": "not_regex",
         "value": r"(?i)\b(i'?ll (do|get|start|look|check)|let me (start|work|look|check)|give me a (moment|minute))\b[^\n]{0,80}$"}
    ok, _ = check_assertion("Done: I checked the calendar.\nAll three meetings are confirmed.", a)
    assert ok is True          # clean multi-line answer passes
    ok, _ = check_assertion("Good question.\nI'll check the calendar now", a)
    assert ok is False         # multi-line promise-tail fails


def test_legacy_episode_ids_are_stable_across_collects():
    from coaching.episodes import _episode
    a = _episode("x", "2026-07-28T10:00:00+00:00", "input text", "output text",
                 stream="interaction_log")
    b = _episode("x", "2026-07-28T10:00:00+00:00", "input text", "output text",
                 stream="interaction_log")
    assert a["id"] == b["id"]
    c = _episode("x", "2026-07-28T10:00:01+00:00", "input text", "output text",
                 stream="interaction_log")
    assert a["id"] != c["id"]


def test_make_llm_cases_derives_assertions():
    issue = {"id": "ci-1", "check": "no_placeholder_sources", "agent": "researcher",
             "episode_ids": ["e1"]}
    eps = {"e1": {"id": "e1", "input": "research X", "output": "see example.com"}}
    cases = verify.make_llm_cases(issue, eps)
    assert len(cases) == 1
    assert any(a["type"] == "not_contains" for a in cases[0]["assertions"])
    # unknown check -> no llm cases
    assert verify.make_llm_cases({"id": "i", "check": "no_error", "episode_ids": []}, {}) == []


# ─────────────────────────── report ───────────────────────────

def test_report_skip_if_nothing(tmp_path, monkeypatch):
    from coaching import report
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(kanban, "KANBAN_PATH", tmp_path / "kanban.json")
    monkeypatch.setattr(kanban, "BEADS_DIR", tmp_path / "no-beads")
    monkeypatch.setattr("coaching.report.VERDICTS_PATH", tmp_path / "verdicts.jsonl")
    md, stats = report.build_daily()
    assert md is None and stats["issues_opened_24h"] == 0


def test_report_renders_yesterday_vs_today(tmp_path, monkeypatch):
    from coaching import report
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(kanban, "KANBAN_PATH", tmp_path / "kanban.json")
    monkeypatch.setattr(kanban, "BEADS_DIR", tmp_path / "no-beads")
    vpath = tmp_path / "verdicts.jsonl"
    monkeypatch.setattr("coaching.report.VERDICTS_PATH", vpath)
    now = datetime.now(timezone.utc)
    rows = []
    for hours_ago, ok in ((30, False), (30, False), (2, True), (2, True), (2, False)):
        ts = (now - timedelta(hours=hours_ago)).isoformat()
        rows.append({"ts": ts, "episode_ts": ts, "agent": "code-bot", "check": "c",
                     "ok": ok, "severity": "high", "verifier": "deterministic"})
    vpath.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    kanban.file_issue(_issue())
    md, stats = report.build_daily(now=now)
    assert md is not None
    assert "Yesterday vs today" in md
    assert "code-bot" in md
    assert "0/2 (0%)" in md          # yesterday
    assert "2/3 (67%)" in md         # today
    assert "Issues opened" in md


# ─────────────────────────── beads backend ───────────────────────────

import shutil as _shutil
import subprocess as _sp


@pytest.mark.skipif(not _shutil.which("bd"), reason="beads not installed")
def test_beads_backend_roundtrip(tmp_path, monkeypatch):
    """file -> move -> pick -> regression link, on a real throwaway bd db."""
    beads_dir = tmp_path / "beads"
    beads_dir.mkdir()
    _sp.run(["bd", "init", "--prefix", "ci", "--quiet"], cwd=beads_dir,
            capture_output=True, timeout=60)
    monkeypatch.setattr(kanban, "BEADS_DIR", beads_dir)
    monkeypatch.setattr(kanban, "COACH_DIR", tmp_path)
    monkeypatch.setattr(kanban, "KANBAN_PATH", tmp_path / "kanban.json")
    assert kanban.backend() == "beads"

    assert kanban.file_issue(_issue())["status"] == "filed"
    assert kanban.file_issue(_issue())["status"] == "duplicate"
    row = kanban.get("ci-1")
    assert row and row["status"] == "backlog" and row["rca_class"] == "tool-defect"

    assert kanban.move("ci-1", "in_review", fix={"branch": "b"})
    assert kanban.get("ci-1")["fix"]["branch"] == "b"
    # in_review maps to bd 'blocked' -> never in the ready queue
    assert kanban.pick_next() is None

    kanban.move("ci-1", "done")
    res = kanban.file_issue(_issue(iid="ci-1"))
    assert res["status"] == "filed" and res["id"] != "ci-1"
    assert kanban.get(res["id"])["regression_of"] == "ci-1"
    assert kanban.board_summary() == {"done": 1, "backlog": 1}
