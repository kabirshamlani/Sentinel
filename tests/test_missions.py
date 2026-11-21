"""Mission engine + timeline tests — fully offline (no LLM, no network, no
real state). Verifies the 2026-08-14 rebuild:

  timeline: idempotent ingest, urgency stamping, multi-source escalation
            (the 'Alex emailed THEN pinged on chat' signal), self/bot
            exclusion, mission-marking.
  missions: scan opens a mission for a hot actionable cluster exactly ONCE,
            below-threshold asks go to backlog not execution, the execute
            result contract parses, delivery proposes the draft reply through
            the broker (never a direct send) addressed from Python-held
            metadata, and a failed run is reported as FAILED — never delivered
            as if done (the 401-as-analysis pathology).

Run: python -m tests.test_missions   (or pytest tests/test_missions.py)
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="sentinel-mission-test-"))

from memory import timeline                      # noqa: E402
from verticals import missions                   # noqa: E402

# ── isolation: private timeline DB, in-memory kv, no real ledger/dirs ──────

timeline._DB_PATH = TMP / "timeline.db"
timeline._local = threading.local()
missions.MISSIONS_DIR = TMP / "missions"

# deterministic identity fusion (no real people roster in tests)
timeline._resolve_person = lambda name: (
    "alex@example.com" if "alex" in (name or "").lower() else "")

# originals, so scan-tests' stubs can be undone for the execution tests
_ORIG = {"_run_mission": missions._run_mission, "_detect": missions._detect,
         "perceive_email": missions.perceive_email,
         "perceive_cursor": missions.perceive_cursor,
         "_day_context": missions._day_context,
         "_record_ledger": missions._record_ledger}


def _restore():
    for k, v in _ORIG.items():
        setattr(missions, k, v)


class FakeKV:
    def __init__(self):
        self.d = {}
    def get(self, k, default=None):
        return self.d.get(k, default)
    def put(self, k, v):
        self.d[k] = v
    def now_iso(self):
        return datetime.now(timezone.utc).isoformat()


class StubRouter:
    def __init__(self):
        self.msgs = []
    async def broadcast(self, text):
        self.msgs.append(text)


class CaptureBroker:
    def __init__(self):
        self.proposals = []
    async def propose(self, action_type, description, details, reasoning, **kw):
        self.proposals.append({"type": action_type, "description": description,
                               "details": details, **kw})
        return {"action_id": f"action-test-{len(self.proposals)}",
                "status": "awaiting_approval"}


def _now(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _seed_alex():
    """The canonical scenario: urgent email, then a chat follow-up."""
    return timeline.ingest([
        {"id": "msg-r1", "source": "gmail", "ts": _now(180),
         "sender": "Alex Rivera", "sender_key": "alex@example.com",
         "channel": "inbox",
         "text": "Common registry endpoint for knowledge base — can you set up a "
                 "common registry endpoint? Need it by today EOD.",
         "meta": {"subject": "Common registry endpoint for knowledge base"}},
        {"id": "sp1/m2", "source": "gchat_dm", "ts": _now(30),
         "sender": "Alex Rivera", "sender_key": "users/12345",
         "channel": "DM: Alex", "text": "have you got the email? expecting this today"},
    ])


# ── timeline ───────────────────────────────────────────────────────────────

def test_ingest_dedup_and_urgency():
    n = _seed_alex()
    assert n == 2, f"expected 2 new rows, got {n}"
    assert _seed_alex() == 0, "re-ingest must be idempotent"
    evs = timeline.recent(hours=6)
    email = next(e for e in evs if e["id"] == "gmail:msg-r1")
    assert email["urgency"] == 3, f"ask+deadline should stamp 3, got {email['urgency']}"
    print("ok: ingest dedup + urgency stamping")


def test_hot_threads_multisource_escalation():
    timeline.ingest([
        {"id": "msg-b1", "source": "gmail", "ts": _now(60),
         "sender": "Some Vendor", "sender_key": "vendor@x.com",
         "channel": "inbox", "text": "Our monthly product changelog is here"},
    ])
    hot = timeline.hot_threads(hours=6)
    assert hot, "expected at least one hot cluster"
    top = hot[0]
    assert top["sender_key"] == "alex@example.com", \
        f"email+chat identities must FUSE into one cluster, got {top['sender_key']}"
    assert set(top["sources"]) == {"gmail", "gchat_dm"}, \
        f"fused cluster must span both sources, got {top['sources']}"
    # urgency 3 + multi-source 1.5 + extra-msg 0.5 + recency 1.0 = 6.0
    assert top["score"] >= 5.5, f"escalating cross-source ask should score >=5.5, got {top['score']}"
    keys = [c["sender_key"] for c in hot]
    assert "vendor@x.com" not in keys, "no-ask newsletter sender must not be hot"
    print(f"ok: identity fusion + multi-source escalation (score {top['score']})")


def test_self_and_bot_excluded():
    timeline.ingest([
        {"id": "msg-s1", "source": "gmail", "ts": _now(5), "kind": "self",
         "sender": "the owner", "sender_key": "owner@example.com",
         "channel": "inbox", "text": "urgent can you fix deploy today asap??"},
        {"id": "msg-n1", "source": "gmail", "ts": _now(5), "kind": "bot",
         "sender": "Gemini", "sender_key": "gemini-notes@google.com",
         "channel": "inbox", "text": "Notes: urgent meeting today please review"},
    ])
    keys = [c["sender_key"] for c in timeline.hot_threads(hours=6)]
    assert "owner@example.com" not in keys
    assert "gemini-notes@google.com" not in keys
    print("ok: self + bot senders excluded from clusters")


def test_mark_mission_hides_cluster():
    before = [c["sender_key"] for c in timeline.hot_threads(hours=6)]
    assert "alex@example.com" in before
    # production (_open_mission) marks the WHOLE cluster's event ids
    timeline.mark_mission(["gmail:msg-r1", "gchat_dm:sp1/m2"], "m-test")
    after = [c["sender_key"] for c in timeline.hot_threads(hours=6)]
    assert "alex@example.com" not in after, "missioned events must leave the hot pool"
    timeline.mark_mission(["gmail:msg-r1", "gchat_dm:sp1/m2"], "")   # un-mark for later tests
    print("ok: mark_mission removes events from the hot pool")


# ── missions: parsing helpers ──────────────────────────────────────────────

def test_sender_parts_and_sections():
    assert missions._sender_parts("Alex S <alex@x.com>") == ("Alex S", "alex@x.com")
    assert missions._sender_parts("alex@x.com")[1] == "alex@x.com"
    sections = missions._split_sections(
        "preamble\n## RESULT\nBuilt the endpoint.\n## EVIDENCE\ncommit abc123\n"
        "## DRAFT_REPLY\nHey Alex — done.\n## FILES\napp.py\nNONE-ish\n")
    assert sections["RESULT"] == "Built the endpoint."
    assert "abc123" in sections["EVIDENCE"]
    assert sections["DRAFT_REPLY"].startswith("Hey Alex")
    print("ok: sender parsing + result-contract sections")


# ── missions: scan behavior ────────────────────────────────────────────────

def _fresh_scan_env(verdict):
    fake_kv = FakeKV()
    missions.kv = fake_kv
    missions._running.clear()

    async def no_email(*a, **k):
        return 0
    async def stub_detect(cluster):
        return verdict
    ran = []
    async def stub_run(mission, broker, router):
        ran.append(mission)
    missions.perceive_email = no_email
    missions.perceive_cursor = no_email
    missions._detect = stub_detect
    missions._run_mission = stub_run
    missions._day_context = lambda: "ctx"
    return fake_kv, ran


def test_scan_opens_once_then_dedups():
    verdict = {"actionable": True, "task": "Set up the knowledge base registry endpoint",
               "deliverable": "endpoint live + reply to Alex", "urgency": 8,
               "kind": "code", "repo": "", "why": "deadline today, two pings"}
    fake_kv, ran = _fresh_scan_env(verdict)
    router, broker = StubRouter(), CaptureBroker()

    res = asyncio.run(missions.scan(broker, router))
    assert len(res["started"]) == 1, f"expected 1 mission, got {res}"
    assert any("Picked up" in m for m in router.msgs), "pickup must be announced"
    mission = list(fake_kv.get("missions").values())[0]
    assert mission["urgency"] == 8 and mission["status"] == "queued", \
        "missions QUEUE for the daemon executor (harness split), never execute inline"

    res2 = asyncio.run(missions.scan(broker, router))
    assert res2["started"] == [], "same cluster must not re-open a mission"

    # the daemon-side executor picks the queued mission up
    ran = []
    async def stub_run(m, b, r):
        ran.append(m["id"])
        missions._running.discard(m["id"])
    missions._run_mission = stub_run

    async def _exec():
        n = await missions.run_queued(broker, router)
        await asyncio.sleep(0)          # let the spawned stub task run
        return n
    n = asyncio.run(_exec())
    assert n == 1 and ran == [mission["id"]]
    assert fake_kv.get("missions")[mission["id"]]["status"] == "executing"
    print("ok: scan queues once, dedups on re-scan, executor starts queued")


def test_scan_backlogs_below_threshold():
    verdict = {"actionable": True, "task": "Look at the dashboard sometime",
               "deliverable": "opinion", "urgency": 3, "kind": "other",
               "repo": "", "why": "no deadline"}
    fake_kv, ran = _fresh_scan_env(verdict)
    # fresh newest-event id so the seen-dedup from the previous test can't hide it
    timeline.ingest([{"id": "sp1/m3", "source": "gchat_dm", "ts": _now(1),
                      "sender": "Alex Rivera", "sender_key": "users/12345",
                      "channel": "DM: Alex", "text": "also can you check the dashboard please"}])
    router, broker = StubRouter(), CaptureBroker()
    res = asyncio.run(missions.scan(broker, router))
    assert res["started"] == [], "urgency 3 must not execute"
    assert fake_kv.get("mission_backlog"), "low-urgency actionable ask must reach the backlog"
    print("ok: below-threshold asks go to backlog, not execution")


# ── missions: execution + delivery ─────────────────────────────────────────

def _mission_fixture():
    return {
        "id": "m-fix1", "created": _now(0), "status": "executing",
        "task": "Set up the knowledge base registry endpoint",
        "deliverable": "endpoint live + reply to Alex",
        "kind": "research", "repo": "", "urgency": 8,
        "sender": "Alex Rivera", "sender_key": "alex@example.com",
        "sources": ["gmail", "gchat_dm"],
        "event_ids": ["gmail:msg-r1", "gchat_dm:sp1/m2"],
        "cluster_text": "PERSON: Alex ...",
    }


def test_run_mission_delivers_draft_via_broker():
    import brain.claude_runner as cr
    _restore()
    fake_kv = FakeKV()
    missions.kv = fake_kv
    missions._day_context = lambda: "ctx"
    missions._record_ledger = lambda *a, **k: None

    fake_out = ("## RESULT\nRegistry endpoint scaffolded and tested locally.\n"
                "## EVIDENCE\ntests: 4 passed\n"
                "## DRAFT_REPLY\nHey Alex — the common registry endpoint is up.\n"
                "## FILES\nNONE\n")

    async def fake_run_claude(prompt, **kw):
        yield cr.ClaudeEvent(type="result", content=fake_out, is_error=False)
    real = cr.run_claude
    cr.run_claude = fake_run_claude
    try:
        router, broker = StubRouter(), CaptureBroker()
        asyncio.run(missions._run_mission(_mission_fixture(), broker, router))
    finally:
        cr.run_claude = real

    assert broker.proposals, "delivery must PROPOSE the reply, never send directly"
    p = broker.proposals[0]
    assert p["type"] == "send_email"
    assert p["details"]["to"] == "alex@example.com", "recipient must come from Python-held metadata"
    assert p["details"]["subject"].startswith("Re: Common registry endpoint")
    assert "registry endpoint is up" in p["details"]["body"]
    assert p.get("criticality_hint") == "critical"
    m = fake_kv.get("missions")["m-fix1"]
    assert m["status"] == "done" and "draft_action" in m["evidence"]
    assert any(x.startswith("✅ Done") for x in router.msgs)
    print("ok: delivery = done-report + broker-gated draft addressed from metadata")


def test_run_mission_failure_is_honest():
    import brain.claude_runner as cr
    _restore()
    fake_kv = FakeKV()
    missions.kv = fake_kv
    missions._day_context = lambda: "ctx"
    missions._record_ledger = lambda *a, **k: None

    async def failing_run_claude(prompt, **kw):
        yield cr.ClaudeEvent(type="result",
                             content="Failed to authenticate. API Error: 401 Unauthorized",
                             is_error=True)
    real = cr.run_claude
    cr.run_claude = failing_run_claude
    try:
        router, broker = StubRouter(), CaptureBroker()
        asyncio.run(missions._run_mission(_mission_fixture(), broker, router))
    finally:
        cr.run_claude = real

    assert not broker.proposals, "a failed mission must not deliver drafts"
    m = fake_kv.get("missions")["m-fix1"]
    assert m["status"] == "failed"
    assert any("Mission failed" in x for x in router.msgs), "failure must be reported honestly"
    print("ok: failed run reports failure — never fakes done")


def test_auth_error_signature_flags_result():
    import brain.claude_runner as cr
    evs = cr._parse_events({"type": "result",
                            "result": "Failed to authenticate. API Error: 401 Unauthorized: "
                                      "Forbidden: Invalid token"})
    assert evs[0].is_error, "the Aug-12 401 result shape must be flagged as error"
    evs2 = cr._parse_events({"type": "result", "result": "All good: " + "x" * 500 +
                             " (note: we fixed the 401 Unauthorized bug)"})
    assert not evs2[0].is_error, "long real analyses mentioning errors must NOT be flagged"
    print("ok: 401-result shape flagged; long genuine text not misflagged")


ALL = [test_ingest_dedup_and_urgency, test_hot_threads_multisource_escalation,
       test_self_and_bot_excluded, test_mark_mission_hides_cluster,
       test_sender_parts_and_sections, test_scan_opens_once_then_dedups,
       test_scan_backlogs_below_threshold, test_run_mission_delivers_draft_via_broker,
       test_run_mission_failure_is_honest, test_auth_error_signature_flags_result]

if __name__ == "__main__":
    for t in ALL:
        t()
    print(f"\n{len(ALL)}/{len(ALL)} mission-engine tests passed")
