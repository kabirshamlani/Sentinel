"""Self-inspection — Sentinel answers 'what am I made of?' by reading itself.

The owner asked the bot "are we using Hermes/OpenClaw?" and it answered from general
knowledge and got it misleadingly wrong. An agent should know its OWN
architecture by inspecting the codebase, not guessing. This builds a live
manifest (modules, agents, channels, verticals, what's active vs dormant) that
the chat handler injects when the owner asks about the system itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from config.paths import REPO_ROOT

# Curated architecture facts — the things that aren't obvious from file names.
# Kept here (not in an LLM) so the answer is always correct and current.
ARCHITECTURE = {
    "orchestration": (
        "HERMES IS THE MAIN ORCHESTRATOR (the owner's decision, 2026-08-14, v0.18): "
        "a locked-down Hermes profile ('sentinel', Claude Sonnet 5 via the "
        "TrueFoundry gateway) is the decision core for BOTH chat turns and the "
        "ambient mission engine. Its only hands are the sentinel MCP bridge: it "
        "spawns CLAUDE CODE subagents (spawn_claude / open_mission) for anything "
        "touching the owner's machine, accounts, or code; reuses or proposes "
        "SPECIALIST agents (list_specialists / create_specialist, creation "
        "gated on the owner's tap); and reaches the world through OpenClaw/Telegram "
        "delivery — always via the approval broker. The thin Python Conductor "
        "daemon remains the SUPERVISOR: perception pollers (gmail/gchat/cursor "
        "→ timeline), credential custody (per-spawn injection, no secrets in "
        "Hermes config), the durable broker + Telegram poller, the mission "
        "EXECUTOR, and an automatic fallback judge when Hermes is down. "
        "Boundaries stay structural: deny-guard on every Claude tier, every "
        "send/PR through the owner's tap, owner-gated ingress."),
    "openclaw": (
        "The multi-channel reach layer (its genuine strength — the only real "
        "always-on multi-channel gateway, installed + running here). BOTH "
        "directions are now wired in channels/openclaw_bridge.py: OUTBOUND via "
        "`openclaw message send` (WhatsApp/iMessage/GChat/Telegram/Discord, native "
        "buttons/media) AND INBOUND via a `openclaw message read` receive loop "
        "(run_receive_loop, started by the daemon) that is owner-gated and feeds "
        "the SAME router->broker->approval path — so the owner can COMMAND Sentinel "
        "from WhatsApp/iMessage, not just receive approvals. It is NOT the brain — "
        "orchestration + the safety broker stay in our Python Conductor (OpenClaw's "
        "ACP runs host-side unsandboxed + releases are churny; ACP stays an "
        "optional approval-gated manual tool, never the autonomous path). HONEST "
        "STATUS: inbound/outbound light up per channel only once the owner QR-links it "
        "(WhatsApp needs the scan); until a channel is linked, OpenClaw for it is a "
        "no-op and Telegram carries everything. Telegram is the working default."),
    "hermes": (
        "THE MAIN ORCHESTRATOR since 2026-08-14 (the owner's decision, v0.18) — no "
        "longer a sidecar. A locked-down profile (`sentinel`, model Claude "
        "Sonnet 5 via the TrueFoundry gateway, toolsets memory/skills/todo + "
        "the sentinel MCP bridge ONLY — no shell/file/exec) is the decision "
        "core for chat AND the ambient mission engine: it judges the timeline, "
        "opens/dismisses missions, spawns Claude Code subagents (spawn_claude / "
        "open_mission), reuses or proposes specialists, and proposes sends via "
        "the broker. Credentials are injected per-spawn (never in its config); "
        "the daemon supervises and falls back to an internal judge if Hermes "
        "is down. Its self-written skills remain untrusted drafts needing "
        "approval."),
    "army": (
        "An orchestrator + specialist army with a real pipeline, not one "
        "generalist. Non-trivial asks flow: INTAKE (understand) → ORCHESTRATOR "
        "(distribute + select agents, read-vs-write) → SPECIALISTS (8: code-bot, "
        "review-bot, researcher, analyst, comms-drafter, ops-watcher, planner, "
        "tester) → REVIEW (independent check + one recheck) → OBSERVER (health/"
        "quality gate) → back to chat. Reads fan out in parallel; writes stay "
        "single-agent; coordination is via a shared blackboard, not handoffs. "
        "The observer watches the agents and feeds recurring failures into a "
        "self-improvement loop that can propose new specialists."),
    "missions": (
        "The autonomous task-pickup engine (v0.17, verticals/missions.py + "
        "memory/timeline.py). Every email (in:inbox, read or not), every Google "
        "Chat message in watched DMs/spaces (ambient — no wake-word needed), and "
        "recent Cursor sessions land in ONE cross-source timeline. The same "
        "person pinging on two channels about one ask FUSES into one cluster "
        "(people-roster identity fusion) and escalates its urgency score. Hot "
        "clusters are LLM-judged (with full email bodies); a real ask at "
        "urgency ≥6 becomes a MISSION that starts EXECUTING within ~10 minutes "
        "of arriving — act-tier Claude Code in an isolated worktree/scratch dir, "
        "parallel to the owner's own work, never idle-gated. Delivery = a done-report "
        "with evidence + the draft reply to the requester as ONE approval tap "
        "(+ a PR proposal when real commits exist). Failures are reported as "
        "failures. Caps: 5/day, 2 concurrent, 14-day dedup. Lower-urgency asks "
        "go to a backlog surfaced in the heartbeat."),
    "safety": (
        "Reads are free (Tier R). Sends/mutations go through the approval broker "
        "(Tier A). Action-types with a proven clean track record earn auto-run "
        "(Tier X). Corrections are sacred — a flagged mistake becomes a "
        "never-repeat rule + permanent memory."),
    "memory": (
        "ONE unified store — memory/gateway.py, a single SQLite+FTS5 DB "
        "(~/.sentinel/memory/unified.db) that is the single source of truth. "
        "Facts, lessons, skills, corrections, errors are all rows with "
        "confidence+provenance+decay; corrections never decay. semantic.py and "
        "reasoning_bank.py are thin facades over it (no scattered stores). "
        "OpenClaw's and Hermes's own memory is READ into this canonical store by "
        "memory/sync.py — nothing is siloed. Nightly consolidation curates in "
        "place (delta-ops on MEMORY.md, no wholesale rewrite)."),
    "models": (
        "9 models live via brain/providers.py + arena.py: Claude Fable5/Opus4.8/"
        "Sonnet5/Haiku4.5 (TrueFoundry gateway), GPT-5.6 sol/terra/luna, Gemini "
        "3.1-pro/3.5-flash. arena.race() runs them head-to-head with an "
        "independent judge and keeps an evidence ledger."),
}


def _summarize_module(path: Path) -> str:
    """First line of the module docstring, if any."""
    try:
        text = path.read_text(encoding="utf-8")[:600]
        m = re.search(r'"""(.+?)(?:\.|\n)', text, re.S)
        return m.group(1).strip()[:100] if m else ""
    except OSError:
        return ""


def list_agents() -> list[dict]:
    out = []
    d = REPO_ROOT / ".claude" / "agents"
    if d.exists():
        for f in sorted(d.glob("*.md")):
            desc = ""
            try:
                m = re.search(r"^description:\s*(.+)$", f.read_text(), re.M)
                desc = (m.group(1).strip() if m else "")[:120]
            except OSError:
                pass
            out.append({"slug": f.stem, "description": desc})
    return out


def list_skills() -> list[str]:
    d = REPO_ROOT / ".claude" / "skills"
    return [p.name for p in sorted(d.iterdir())] if d.exists() else []


def module_map() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for f in sorted(REPO_ROOT.rglob("*.py")):
        if any(part.startswith(".") for part in f.parts) or f.name == "__init__.py":
            continue
        rel = f.relative_to(REPO_ROOT)
        pkg = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        groups.setdefault(pkg, []).append(
            {"module": str(rel), "summary": _summarize_module(f)})
    return groups


def active_channels() -> dict:
    """What's actually live right now (checks config/keychain, not just code)."""
    from channels.telegram import TelegramChannel
    from tools import gog_workspace as gw
    import shutil
    tg = TelegramChannel()
    return {
        "telegram": bool(tg.configured),
        "gchat": gw.available(),
        "openclaw": shutil.which("openclaw") is not None,
    }


def manifest_text(topic: str = "") -> str:
    """Compact architecture answer injected into the chat prompt.

    If `topic` matches a known area (hermes, openclaw, army, memory, models,
    safety, orchestration) return that specific fact; else a full overview."""
    low = topic.lower()
    for key, fact in ARCHITECTURE.items():
        if key in low or (key == "army" and re.search(r"\b(army|agents?|specialist|orchestrat)\b", low)) \
                or (key == "missions" and re.search(r"\b(missions?|task.?pickup|autonomous|pick(s|ed)? up)\b", low)):
            return f"[Sentinel self-knowledge — {key}]\n{fact}"

    agents = list_agents()
    ch = active_channels()
    groups = module_map()
    lines = ["[Sentinel self-knowledge — architecture]"]
    lines.append(ARCHITECTURE["orchestration"])
    lines.append(f"\nSpecialist agents ({len(agents)}): " +
                 ", ".join(a["slug"] for a in agents))
    lines.append(f"Channels — telegram: {'live' if ch['telegram'] else 'off'}, "
                 f"gchat: {'live' if ch['gchat'] else 'off'}, "
                 f"openclaw/whatsapp: {'available' if ch['openclaw'] else 'dormant'}")
    lines.append(f"Hermes: {ARCHITECTURE['hermes']}")
    lines.append(f"\nModules: {sum(len(v) for v in groups.values())} across "
                 + ", ".join(f"{k}({len(v)})" for k, v in sorted(groups.items())))
    lines.append(ARCHITECTURE["safety"])
    return "\n".join(lines)


def is_self_question(text: str) -> bool:
    """Heuristic: is the owner asking about Sentinel itself?"""
    low = text.lower()
    # Strong signals that always indicate a self-question regardless of subject.
    if re.search(r"\b(hermes|openclaw|are we using|do you use|are you (using|an|a)|"
                 r"how are you (built|made)|what are you made of|your architecture|"
                 r"one agent|single agent|army of|orchestrat)\b", low):
        return True
    subject = re.search(r"\b(you|your|yourself|sentinel|the (system|bot|agent|army)|"
                        r"\bwe\b|\bus\b|this (system|bot|agent))\b", low)
    topic = re.search(r"\b(architecture|made of|built|using|use|agents?|specialist|"
                      r"models?|memory|capabilit|structure|components?|how do you|"
                      r"what can you|what do you)\b", low)
    return bool(subject and topic)
