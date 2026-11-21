"""Self-repair loop + attachment guard tests (offline, no real kanban/kv).

Covers the 2026-08-15 directives:
  - a live task failure files a coaching issue AND flags the ASAP fix cycle
  - email attachments are validated hard (an email must never silently lack
    the file it promises — the resume bug) and never leak credential files
  - PM full-throttle: gates widen to the working system but the GATE PATHS
    (approval broker, deny-guard, the PM itself) can never self-merge

Run: python -m tests.test_self_repair   (or pytest)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_live_failure_files_issue_and_flags_asap():
    from coaching import escalate

    filed, kv_store = [], {}

    class FakeKanban:
        @staticmethod
        def file_issue(issue):
            filed.append(issue)
            return {"status": "filed", "id": issue["id"]}

    class FakeKV:
        @staticmethod
        def put(k, v):
            kv_store[k] = v
        @staticmethod
        def get(k, default=None):
            return kv_store.get(k, default)

    import coaching, conductor
    real_kanban = getattr(coaching, "kanban", None)
    real_kv = getattr(conductor, "kv", None)
    coaching.kanban = FakeKanban
    conductor.kv = FakeKV
    try:
        iid = escalate.live_failure(
            "agent-chat", "mail resume to the owner",
            "attach failed for /Users/you/Desktop/r1.pdf after 3 retries id a1b2c3d4e5")
        assert iid and filed, "a live failure must file a coaching issue"
        issue = filed[0]
        assert issue["severity"] == "high" and issue["check"] == "live_failure"
        assert "attach failed" in issue["evidence"][0]["evidence"]
        assert kv_store.get("coach_fix_asap"), "the ASAP flag must summon the fixer"

        # same error CLASS (different path/count/hex) → same fingerprint
        escalate.live_failure(
            "agent-chat", "mail resume to the owner",
            "attach failed for /tmp/other/file.pdf after 7 retries id ffee99887766")
        assert filed[1]["fingerprint"] == filed[0]["fingerprint"], \
            "volatile-stripped error class must produce a stable fingerprint"
    finally:
        if real_kanban is not None:
            coaching.kanban = real_kanban
        else:
            delattr(coaching, "kanban")
        if real_kv is not None:
            conductor.kv = real_kv
        else:
            delattr(conductor, "kv")
    print("ok: live failure -> issue filed + ASAP flag + stable fingerprint")


def test_request_capability_files_feature_issue_and_flags_asap():
    """The organ that makes 'implement X in yourself' real: it must file a
    FEATURE-channel issue the fixer can drain, and summon the fixer ASAP —
    with NO human in the loop between the ask and the filed work."""
    from coaching import escalate

    filed, kv_store = [], {}

    class FakeKanban:
        @staticmethod
        def file_issue(issue):
            filed.append(issue)
            return {"status": "filed", "id": issue["id"]}

    class FakeKV:
        @staticmethod
        def put(k, v):
            kv_store[k] = v
        @staticmethod
        def get(k, default=None):
            return kv_store.get(k, default)

    import coaching, conductor
    real_kanban = getattr(coaching, "kanban", None)
    real_kv = getattr(conductor, "kv", None)
    coaching.kanban = FakeKanban
    conductor.kv = FakeKV
    try:
        iid = escalate.request_capability(
            "persistent tmux subagent sessions",
            "spawn_claude reuses a warm tmux session per tier instead of cold-"
            "starting a subprocess; sessions idle-reap after N minutes; deny-guard "
            "still blocks sends/push inside them",
            target="brain/claude_runner.py")
        assert iid and filed, "a capability request must file a coaching issue"
        issue = filed[0]
        # routed onto the FEATURE channel so the fixer writes an acceptance
        # test first and the feature_impl gate applies — NOT the defect path
        assert issue["fix_channel"] == "feature"
        assert issue["rca_class"] == "feature"
        assert issue["check"] == "capability_request"
        assert issue["severity"] == "high"
        assert issue["fix_target"] == "brain/claude_runner.py"
        assert "tmux" in issue["evidence"][0]["evidence"]
        assert kv_store.get("coach_fix_asap"), "the fixer must be summoned ASAP"

        # re-asking the SAME capability dedups (same spec -> same fingerprint)
        escalate.request_capability(
            "tmux sessions",
            "spawn_claude reuses a warm tmux session per tier instead of cold-"
            "starting a subprocess; sessions idle-reap after N minutes; deny-guard "
            "still blocks sends/push inside them")
        assert filed[1]["fingerprint"] == filed[0]["fingerprint"], \
            "the same capability spec must dedup to one request"

        # a DIFFERENT capability must NOT collide
        escalate.request_capability("dark mode dashboard",
                                    "the dashboard renders a dark theme toggle")
        assert filed[2]["fingerprint"] != filed[0]["fingerprint"], \
            "distinct capabilities must get distinct fingerprints"
    finally:
        if real_kanban is not None:
            coaching.kanban = real_kanban
        else:
            delattr(coaching, "kanban")
        if real_kv is not None:
            conductor.kv = real_kv
        else:
            delattr(conductor, "kv")
    print("ok: self-improvement request -> feature issue + ASAP flag, dedups by spec")


def test_feature_gate_rejects_test_only_diff():
    """A capability must ship real implementation. If the fixer's diff touches
    only its own acceptance test (a green light with nothing behind it), the
    feature_impl gate must reject it — the one gaming route a defect repro,
    which runs against unchanged production code, doesn't have."""
    from coaching import verify

    issue = {"id": "ci-feat1234", "rca_class": "feature", "fix_channel": "feature"}

    # test-only: the acceptance test and another test, no implementation -> FAIL
    test_only = ["tests/coaching_repro/test_ci_feat1234.py", "tests/test_helpers.py"]
    g = verify.feature_gate(test_only, issue)
    assert g["passed"] is False and "only tests" in g["reason"]

    # real capability: implementation file + its acceptance test -> PASS
    with_impl = ["brain/claude_runner.py", "tests/coaching_repro/test_ci_feat1234.py"]
    g = verify.feature_gate(with_impl, issue)
    assert g["passed"] is True and "brain/claude_runner.py" in g["impl_files"]
    print("ok: feature gate rejects test-only diffs, accepts real implementation")


def test_attachment_guard():
    from approval.action_queue import _safe_attachments

    tmp = Path(tempfile.mkdtemp(prefix="attach-test-", dir=Path.home() / "Desktop"))
    good = tmp / "report.pdf"
    good.write_bytes(b"%PDF-1.4 test")
    try:
        ok, rejected = _safe_attachments([str(good)])
        assert ok == [str(good)] and not rejected, f"normal file must pass: {rejected}"

        cases = ["~/.ssh/id_rsa", "~/.aws/credentials", "~/.truefoundry/api-key.txt",
                 "/etc/passwd", str(tmp / "nope.pdf"), "~/Desktop/.env"]
        ok, rejected = _safe_attachments(cases)
        assert not ok and len(rejected) == len(cases), \
            f"all sensitive/missing paths must be rejected, got ok={ok}"
        print("ok: attachment guard passes real files, rejects secrets/missing/outside-home")
    finally:
        good.unlink(missing_ok=True)
        tmp.rmdir()


def test_send_email_refuses_on_rejected_attachment():
    from approval import action_queue as aq
    res = aq._exec_send_email({"to": "x@example.com", "subject": "t", "body": "b",
                               "attachments": ["~/.ssh/id_rsa"]})
    assert "error" in res and "rejected" in res["error"], \
        "a rejected attachment must FAIL the send, never send without the file"
    print("ok: send fails loudly on rejected attachment (never silently attachment-less)")


def test_pm_gate_paths_never_self_merge():
    os.environ["SENTINEL_SELF_REPAIR"] = "full"
    from coaching import pm
    prefixes = pm.AUTO_MERGE_PREFIXES + pm.FULL_MODE_EXTRA_PREFIXES
    denied = pm.DENY_WITHIN + pm.GATE_PATHS
    assert any("tools/".startswith(p) or p == "tools/" for p in prefixes), \
        "full mode must widen to tools/"
    for gate in ("approval/broker.py", ".claude/hooks/deny-guard.sh",
                 "coaching/pm.py", "config/paths.py"):
        assert any(gate == d or gate.startswith(d) for d in denied), \
            f"gate path {gate} must NEVER be auto-mergeable"
    cfg = pm.config()
    assert cfg["window_hours"] == 0.0 and cfg["daily_cap"] >= 10, \
        "full mode: no silence wait, higher cap"
    print("ok: PM full throttle widens surface; gates can never rewrite themselves")


ALL = [test_live_failure_files_issue_and_flags_asap,
       test_request_capability_files_feature_issue_and_flags_asap,
       test_feature_gate_rejects_test_only_diff,
       test_attachment_guard,
       test_send_email_refuses_on_rejected_attachment,
       test_pm_gate_paths_never_self_merge]

if __name__ == "__main__":
    for t in ALL:
        t()
    print(f"\n{len(ALL)}/{len(ALL)} self-repair tests passed")
