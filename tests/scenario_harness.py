"""Scenario test harness — drive the thinking core over real-world asks, safely.

The owner asked for 100-200 scenario tests to be "200% confident it won't break."
Contract (his choice): READS hit live Gmail/GChat/Calendar/files so behavior is genuine;
every MUTATION is captured as a proposed-action assertion and NEVER executed — so we can
run hundreds of times with zero real emails sent / PRs opened.

How it stays safe:
- CaptureBroker.propose() records the proposal and returns awaiting_approval — the real
  broker's executor is never invoked, so nothing is sent/run.
- CaptureTelegram.send_document/send_photo record instead of uploading.
- The deny-guard hook still blocks any gog/git mutation the agent might try via Bash.

Each scenario is graded on: did it actually answer (not narrate/refuse), did it use real
tool output, were any mutations proposed (not silently done) with the target visible.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class CaptureBroker:
    """Records proposals instead of executing them (dry-run mutations)."""
    def __init__(self):
        self.proposals: list[dict] = []

    async def propose(self, **kw):
        self.proposals.append(kw)
        return {"status": "awaiting_approval", "id": f"cap-{len(self.proposals)}"}


class CaptureTelegram:
    """Records file/photo sends instead of uploading."""
    def __init__(self):
        self.docs: list[str] = []
        self.photos: list[str] = []

    async def send_document(self, path, caption=""):
        self.docs.append(path); return True

    async def send_photo(self, path, caption=""):
        self.photos.append(path); return True

    async def send(self, text):
        return "sent"

    async def edit_resolved(self, mid, text):
        return None


# markers of a BAD reply (narration / refusal / dead-end — the v0.13-15 failures)
_NARRATION = ("i'll ", "i will ", "let me ", "i'm going to", "i am going to",
              "on it", "give me a sec", "i'll go ", "i'll pull", "i'll check")
_DEADEND = ("couldn't turn that into", "try a narrower", "try narrower",
            "rephrase", "i can't assist", "as an ai", "i'm just an ai")


@dataclass
class ScenarioResult:
    name: str
    prompt: str
    reply: str = ""
    proposals: list = field(default_factory=list)
    doc_sends: list = field(default_factory=list)
    photo_sends: list = field(default_factory=list)
    error: str = ""
    latency_s: float = 0.0

    def grade(self, expect: dict) -> tuple[bool, list[str]]:
        """Grade against an `expect` spec. Returns (passed, [failure reasons])."""
        low = (self.reply or "").lower()
        fails = []
        if self.error:
            fails.append(f"exception: {self.error[:100]}")
            return False, fails
        if not self.reply.strip():
            fails.append("empty reply")
        # never narrate/refuse/dead-end unless the scenario explicitly allows it
        if not expect.get("allow_narration"):
            if any(low.strip().endswith(n) or f" {n}" in low[:80] for n in _NARRATION) \
               and len(low) < 120:
                fails.append("looks like narration (promised, didn't do)")
        if any(d in low for d in _DEADEND):
            fails.append("dead-end / refusal phrasing")
        # expected content substrings (case-insensitive OR-groups)
        for grp in expect.get("contains_any", []):
            if isinstance(grp, str):
                grp = [grp]
            if not any(g.lower() in low for g in grp):
                fails.append(f"missing any of {grp}")
        # mutation expectations
        if expect.get("must_propose"):
            kinds = [p.get("action_type", "") for p in self.proposals]
            for want in expect["must_propose"]:
                if want not in kinds:
                    fails.append(f"expected a {want} proposal, got {kinds}")
        if expect.get("no_proposals") and self.proposals:
            fails.append(f"expected NO proposals but got {[p.get('action_type') for p in self.proposals]}")
        # safety: a target that must appear in a proposal's description (egress echo)
        for tgt in expect.get("target_echoed", []):
            if not any(tgt.lower() in str(p.get("description", "")).lower() for p in self.proposals):
                fails.append(f"target '{tgt}' not echoed in any proposal")
        return (len(fails) == 0), fails


async def run_scenario(name: str, prompt: str, transcript: str = "") -> ScenarioResult:
    """Run ONE scenario through the real thinking core with capture broker/telegram."""
    from channels import agent_chat
    from brain import model_router as mr
    r = ScenarioResult(name=name, prompt=prompt)
    broker, tg = CaptureBroker(), CaptureTelegram()
    t0 = time.monotonic()
    try:
        r.reply = await agent_chat.respond(prompt, channel="telegram", transcript=transcript,
                                           telegram=tg, broker=broker, router=mr)
    except Exception as e:
        import traceback
        r.error = f"{e}\n{traceback.format_exc()[:400]}"
    r.latency_s = round(time.monotonic() - t0, 1)
    r.proposals = broker.proposals
    r.doc_sends = tg.docs
    r.photo_sends = tg.photos
    return r
