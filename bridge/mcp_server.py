"""Sentinel MCP bridge — the structured hands Hermes gets as the main harness.

ARCHITECTURE DECISION (the owner, 2026-08-14): "I want Hermes as the main harness."
Hermes (profile `sentinel`, toolsets locked to memory/skills/todo) is the
always-on decision core: it reads the cross-source timeline, judges what
matters, opens missions, and reports. This MCP server is the ONLY way it acts
on the world — every tool here lands on the same structural rails as before:

  * mutations  → the approval broker (the owner's tap executes, never the model)
  * execution  → Claude Code missions, run by the daemon in isolated worktrees
  * delivery   → the owner's own Telegram (keychain chat id — hardcoded egress)

Hermes never sees credentials (they live in this process / the daemon), never
gets terminal/file/exec toolsets, and cannot send anything directly. The
harness thinks; the rails act. (knowledge base DOK-4 #1: boundaries are structural,
never prompt-based — that survives the harness swap.)

Run: python3 -m bridge.mcp_server   (stdio; registered via `hermes mcp add`)
"""

# NB: no `from __future__ import annotations` here — it stringifies type
# hints, and FastMCP's tool introspection needs real classes to build schemas.

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("sentinel-bridge")

mcp = FastMCP("sentinel")

# Action types the harness may propose directly (everything else must go
# through a mission, whose delivery path builds its own proposals). All of
# these are broker-gated; send_* are CRITICAL_TYPES (held for the owner forever).
_PROPOSABLE = {"send_email", "send_gchat", "create_issue", "schedule_meeting",
               "draft_email", "draft_gchat"}

_router = None
_broker = None


def _rails():
    """Broker + router for THIS process (send-only: the daemon owns the
    Telegram poller and button-tap resolution; proposals persist to the shared
    on-disk queue, so cross-process approval works — the tested v0.2 design)."""
    global _router, _broker
    if _router is None:
        from approval import broker as bk
        from channels.telegram import TelegramChannel
        from channels.router import ChannelRouter
        _broker = bk.ApprovalBroker()
        _router = ChannelRouter(_broker, telegram=TelegramChannel())
        _broker._deliver = _router.deliver_approval
        _broker._notify = _router.notify_resolved
    return _broker, _router


@mcp.tool()
def timeline_hot() -> str:
    """Ranked hot clusters from the cross-source timeline: people whose recent
    email/chat activity looks like it needs attention. Each cluster shows
    sender, sender_key (use it in open_mission/dismiss_cluster), score,
    sources, and the chronological story. Clusters you already judged
    (opened/dismissed) are filtered out until they get a NEW message."""
    from memory import timeline
    from conductor import kv
    seen = kv.get("mission_seen", {}) or {}
    hot = [c for c in timeline.hot_threads()
           if c["events"][-1]["id"] not in seen]
    if not hot:
        return "No hot clusters right now."
    parts = []
    for c in hot:
        parts.append(f"sender_key: {c['sender_key']}\n"
                     + timeline.render_cluster(c, max_chars=2500))
    return "\n\n────────\n\n".join(parts)


@mcp.tool()
def timeline_thread(person: str) -> str:
    """Everything one person sent recently across all sources (email + chat),
    oldest first. `person` is a sender_key from timeline_hot (an email address
    or users/<id>)."""
    from memory import timeline
    evs = timeline.thread_view(person)
    if not evs:
        return f"No recent events from {person}."
    lines = []
    for e in evs:
        ts = (e.get("ts") or "")[:16].replace("T", " ")
        lines.append(f"[{ts} · {e['source']}] {e.get('text','')[:500]}")
    return "\n".join(lines)[:6000]


@mcp.tool()
def day_context() -> str:
    """the owner's current day: goals, meetings, tasks, and what he's actively
    working on (ambient signals). Read this before judging urgency."""
    from verticals.missions import _day_context
    return _day_context()


@mcp.tool()
async def open_mission(sender_key: str, task: str, deliverable: str,
                       urgency: int, why: str, kind: str = "other",
                       repo: str = "") -> str:
    """Open a mission for a hot cluster: Sentinel EXECUTES the task now
    (Claude Code in an isolated worktree) and delivers a done-report plus a
    ready-to-send draft reply for the owner's one-tap approval. Use for real asks
    with urgency >= 6. kind: code|research|document|reply_only|other.
    repo: owner/repo when clearly about a known repository."""
    from memory import timeline
    from verticals import missions
    from conductor import kv

    cluster = next((c for c in timeline.hot_threads()
                    if c["sender_key"] == sender_key), None)
    if cluster is None:
        return f"ERROR: no hot cluster for sender_key {sender_key!r} — call timeline_hot first."

    verdict = {"actionable": True, "task": task, "deliverable": deliverable,
               "urgency": int(urgency), "kind": kind, "repo": repo, "why": why}
    mission, reason = missions.queue_from_cluster(cluster, verdict)
    if mission is None:
        return f"NOT OPENED: {reason}"

    seen = kv.get("mission_seen", {}) or {}
    seen[cluster["events"][-1]["id"]] = {"at": kv.now_iso(), "actionable": True}
    kv.put("mission_seen", seen)

    _, router = _rails()
    await router.broadcast(
        f"🎯 Picked up: {task}\nFrom: {cluster.get('sender','?')} "
        f"(urgency {urgency}/10 — {why})\nWorking on it now; you'll get the "
        f"result + a ready draft, not questions.")
    return f"Mission {mission['id']} queued — the daemon starts executing it within a minute."


@mcp.tool()
def dismiss_cluster(sender_key: str, reason: str, backlog: bool = False,
                    task: str = "", urgency: int = 0) -> str:
    """Record a judgment that a hot cluster is NOT worth a mission right now
    (FYI, bot noise, already handled, social). Set backlog=true for a real but
    low-urgency ask (give task+urgency) so it surfaces once in the owner's
    heartbeat instead of dying silently."""
    from memory import timeline
    from conductor import kv

    cluster = next((c for c in timeline.hot_threads()
                    if c["sender_key"] == sender_key), None)
    if cluster is None:
        return f"No hot cluster for {sender_key!r} (may already be handled)."
    seen = kv.get("mission_seen", {}) or {}
    seen[cluster["events"][-1]["id"]] = {"at": kv.now_iso(), "actionable": False,
                                         "reason": reason[:200]}
    kv.put("mission_seen", seen)
    if backlog and task:
        bl = kv.get("mission_backlog", []) or []
        bl.append({"at": kv.now_iso(), "task": task[:200],
                   "sender": cluster.get("sender", ""), "urgency": int(urgency)})
        kv.put("mission_backlog", bl[-20:])
        return f"Dismissed for now; added to the heartbeat backlog: {task[:80]}"
    return f"Dismissed: {reason[:120]}"


@mcp.tool()
def mission_status() -> str:
    """Recent missions and their states (queued/executing/done/failed) with
    evidence. Check before opening a duplicate."""
    from conductor import kv
    missions_kv = kv.get("missions", {}) or {}
    if not missions_kv:
        return "No missions yet."
    rows = sorted(missions_kv.values(), key=lambda m: m.get("created", ""))[-10:]
    return "\n".join(
        f"[{m.get('created','')[:16]}] {m['status'].upper():<9} {m['task'][:90]} "
        f"(from {m.get('sender','?')}"
        + (f"; evidence: {m['evidence'][:60]}" if m.get("evidence") else "") + ")"
        for m in rows)


@mcp.tool()
async def propose(action_type: str, description: str, details_json: str,
                  reasoning: str) -> str:
    """Propose a mutation through the approval broker (The owner taps to execute;
    sends are held until he approves — always). details_json examples:
    send_email {"to","subject","body","attachments":["/abs/path.pdf"]} ·
    send_gchat {"space","message"} · create_issue {"owner","repo","title","body"}.
    Attachments must be real files in the owner's home (credential dirs and
    secret-shaped names are refused, and a rejected attachment fails the send
    loudly rather than sending without it)."""
    if action_type not in _PROPOSABLE:
        return (f"REFUSED: {action_type!r} is not harness-proposable. "
                f"Allowed: {sorted(_PROPOSABLE)}. Real work goes through open_mission.")
    try:
        details = json.loads(details_json)
        assert isinstance(details, dict)
    except Exception:
        return "ERROR: details_json must be a JSON object."
    broker, _ = _rails()
    res = await broker.propose(action_type, description[:200], details,
                               reasoning[:400], project="hermes-harness",
                               source="hermes", criticality_hint="critical")
    status = (res or {}).get("status", "unknown")
    aid = (res or {}).get("action_id") or (res or {}).get("id", "")
    return f"Proposed ({status}) action_id={aid} — the owner decides on Telegram."


@mcp.tool()
async def notify(text: str) -> str:
    """Send a plain message to the owner's own Telegram. Use sparingly — silence
    is a feature; never repeat what you already said."""
    _, router = _rails()
    await router.broadcast(text[:3500])
    return "delivered"


@mcp.tool()
async def spawn_claude(task: str, tier: str = "read", agent: str = "",
                       minutes: int = 8) -> str:
    """Spawn a Claude Code subagent NOW and return its result. This is your
    execution hand for chat-scale work: reading/searching the owner's machine and
    accounts (tier=read) or running commands/writing files in a scratch dir
    (tier=act — deny-guard still blocks sends/push/rm). agent: optional
    specialist slug from list_specialists (researcher, analyst, code-bot, …).
    For multi-step deliverables with a requester, prefer open_mission instead."""
    import asyncio as _aio
    if tier not in ("read", "act"):
        return "ERROR: tier must be read or act"
    minutes = max(1, min(int(minutes), 15))
    from pathlib import Path as _P
    scratch = _P.home() / ".sentinel" / "scratch" / "hermes-spawn"
    scratch.mkdir(parents=True, exist_ok=True)
    from brain.claude_runner import run_claude

    async def _go() -> tuple[str, bool]:
        text, err = "", False
        async for ev in run_claude(task, agent=(agent or "conductor"), tier=tier,
                                   working_dir=str(scratch), max_turns=25,
                                   task_type="research", episode_source="hermes-spawn"):
            if ev.type == "result":
                text = ev.content or ""
                err = bool(getattr(ev, "is_error", False))
            elif ev.type == "error":
                text, err = ev.content or "runner error", True
        return text, err

    try:
        text, err = await _aio.wait_for(_go(), timeout=minutes * 60)
    except _aio.TimeoutError:
        return f"ERROR: subagent exceeded {minutes} min and was stopped"
    if err:
        return f"SUBAGENT FAILED: {(text or 'no output')[:400]}"
    return text[:8000] or "(subagent returned empty output)"


@mcp.tool()
def list_specialists() -> str:
    """The reusable specialist agents available to spawn_claude (name + what
    it's for). Check here (and recall) BEFORE creating a new specialist —
    reuse beats duplication."""
    from config.paths import REPO_ROOT
    agents_dir = REPO_ROOT / ".claude" / "agents"
    rows = []
    for f in sorted(agents_dir.glob("*.md")):
        desc = ""
        try:
            for line in f.read_text().splitlines()[:8]:
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
                    break
        except OSError:
            continue
        rows.append(f"{f.stem}: {desc[:140]}")
    return "\n".join(rows) or "(no specialists found)"


@mcp.tool()
async def create_specialist(name: str, description: str, instructions: str) -> str:
    """Propose a NEW reusable specialist agent for a task type you'll see
    again (The owner approves creation with a tap; it then appears in
    list_specialists forever). After proposing, store a `remember` note about
    when to use it. Name: short-kebab-case."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9-]", "-", name.strip().lower())[:40].strip("-")
    if not slug:
        return "ERROR: unusable name"
    md = (f"---\nname: {slug}\ndescription: {description.strip()[:200]}\n---\n\n"
          f"{instructions.strip()[:4000]}\n")
    broker, _ = _rails()
    res = await broker.propose(
        "create_agent",
        f"New specialist agent: {slug}",
        {"agent_path": f".claude/agents/{slug}.md", "content": md},
        f"Hermes harness wants a reusable specialist for: {description[:200]}",
        project="hermes-harness", source="hermes", criticality_hint="critical")
    aid = (res or {}).get("action_id") or (res or {}).get("id", "")
    return (f"Proposed specialist '{slug}' (action {aid}) — exists after the owner "
            "approves. Store a remember note about when to reuse it.")


@mcp.tool()
async def propose_self_improvement(title: str, spec: str, target: str = "") -> str:
    """The owner asked Sentinel to CHANGE ITSELF — add a capability, fix a
    limitation, adopt a suggestion ("implement X in yourself", "you should be
    able to…", "add tmux sessions for subagents"). Route it HERE instead of
    just describing what you'd do: this files the request into Sentinel's own
    repair loop, which builds it for real.

    What happens next, autonomously: the coaching fixer picks it up, writes an
    acceptance test that encodes `spec` (it fails now because the capability is
    absent), implements the capability in an isolated worktree, then runs the
    full gate stack (acceptance test passes, no new test failures, no measuring-
    stick tampering, the diff ships real implementation not just a test, fresh-
    context code review). It proposes the verified change to the owner on Telegram;
    on approval the daemon merges it and self-restarts to run the new code.

    Use for changes to Sentinel's OWN code/behavior — NOT for the owner's work
    tasks (those are open_mission) and NOT for one-off sends (propose).

      title:  short label ("persistent tmux subagent sessions")
      spec:   the observable behavior to build, concretely enough that a test
              can assert it ("spawn_claude reuses a warm tmux session per tier
              instead of cold-starting a subprocess; sessions idle-reap after
              N minutes; deny-guard still blocks sends/push inside them")
      target: optional module hint ("brain/claude_runner.py, channels/…")

    Returns the filed issue id. This is a real, gated code change to Sentinel
    itself — it lands only after the owner approves the verified diff.
    """
    from coaching import escalate
    iid = escalate.request_capability(title[:120], spec[:2000], target[:200],
                                      requested_by="hermes-orchestrator")
    if not iid:
        return ("ERROR: couldn't file the self-improvement request (the repair "
                "loop may be offline). Tell the owner it needs a look.")
    _, router = _rails()
    await router.broadcast(
        f"🛠️ Self-improvement queued: {title[:100]}\n"
        f"I'm building this into myself now (issue {iid}). You'll get a verified "
        f"diff to approve when it passes its gates — I won't auto-apply changes "
        f"to my own code.")
    return (f"Filed self-improvement request {iid} on the FEATURE channel. The "
            f"fixer will build it acceptance-test-first and propose the verified "
            f"diff to the owner. Tell the owner it's queued; don't re-file the same ask.")


@mcp.tool()
def recall(query: str) -> str:
    """Search Sentinel's unified long-term memory (facts, corrections,
    lessons, preferences, past decisions)."""
    from memory import gateway
    return gateway.prompt_block(context_hint=query, budget_chars=1800) or "No matches."


@mcp.tool()
def remember(text: str) -> str:
    """Store a durable fact/decision in the unified memory (secret-scrubbed).
    Use for judgments worth keeping ('Alex's registry asks are always mine')."""
    from memory import gateway
    gateway.remember(text[:600], kind="note", source="hermes-harness")
    return "stored"


if __name__ == "__main__":
    mcp.run()
