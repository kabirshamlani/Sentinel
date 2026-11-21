"""Agent registry — the one map of every agent across all three runtimes.

The owner: "openclaw can have its own agent, hermes will have its all agents, claude
code will have its own, and all work as army." This is the single catalog the
orchestrator consults to route work to the RIGHT runtime+agent, so the army is
coherent instead of three disconnected tools.

Each entry: name, runtime (claude_code | openclaw | hermes | conductor), the
domains it's good at, whether it can mutate, and how to invoke it. The
orchestrator picks by (domain match + read/write + runtime strength):
  * claude_code specialists  -> reasoning/coding/analysis (sandboxed, our default)
  * openclaw agents          -> channel-native tasks + always-on reach
  * hermes                   -> skill generation + cross-session recall (enrichment)
Availability is resolved live (binary/dir present), so the catalog reflects
what's actually runnable right now.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from config.paths import REPO_ROOT


def _claude_specialists() -> list[dict]:
    out = []
    d = REPO_ROOT / ".claude" / "agents"
    if d.exists():
        import re
        for f in sorted(d.glob("*.md")):
            desc = domains = ""
            try:
                txt = f.read_text()
                m = re.search(r"^description:\s*(.+)$", txt, re.M)
                desc = (m.group(1).strip() if m else "")[:160]
            except OSError:
                pass
            out.append({"name": f.stem, "runtime": "claude_code",
                        "domains": _infer_domains(f.stem, desc),
                        "can_mutate": f.stem in ("code-bot", "tester"),
                        "invoke": f"run_claude(agent={f.stem})", "desc": desc})
    return out


def _infer_domains(slug: str, desc: str) -> list[str]:
    table = {"researcher": ["research"], "analyst": ["analysis"],
             "comms-drafter": ["comms"], "ops-watcher": ["ops"],
             "planner": ["planning", "coding"], "tester": ["testing", "coding"],
             "code-bot": ["coding"], "review-bot": ["review"],
             "workspace-assistant": ["comms", "analysis"],
             "synthesizer": ["synthesis"], "data-extractor": ["extraction", "analysis"]}
    return table.get(slug, ["general"])


def _openclaw_agents() -> list[dict]:
    if shutil.which("openclaw") is None:
        return []
    agents = []
    d = Path.home() / ".openclaw" / "agents"
    if d.exists():
        for sub in sorted(d.iterdir()):
            if sub.is_dir():
                agents.append({"name": f"openclaw:{sub.name}", "runtime": "openclaw",
                               "domains": ["channels", "delivery", "reach"],
                               "can_mutate": False,   # delivery only from our side
                               "invoke": f"openclaw agent --agent {sub.name}",
                               "desc": "OpenClaw agent (channel/delivery)."})
    if not agents:  # at least the default
        agents.append({"name": "openclaw:main", "runtime": "openclaw",
                       "domains": ["channels", "delivery", "reach"], "can_mutate": False,
                       "invoke": "openclaw agent --agent main", "desc": "OpenClaw gateway agent."})
    return agents


def _hermes_agents() -> list[dict]:
    try:
        from brain import hermes_adapter
        if not hermes_adapter.available():
            return []
    except Exception:
        return []
    return [{"name": "hermes:skill-gen", "runtime": "hermes",
             "domains": ["skill_generation", "recall"], "can_mutate": False,
             "invoke": "hermes_adapter.draft_skill / recall",
             "desc": "Hermes: generates reusable skills + cross-session recall (enrichment; drafts need approval)."}]


def all_agents() -> list[dict]:
    return _claude_specialists() + _openclaw_agents() + _hermes_agents()


def by_domain(domain: str) -> list[dict]:
    return [a for a in all_agents() if domain in a.get("domains", [])]


def summary() -> str:
    agents = all_agents()
    by_rt: dict[str, list] = {}
    for a in agents:
        by_rt.setdefault(a["runtime"], []).append(a["name"])
    lines = [f"🪖 **The army — {len(agents)} agents across {len(by_rt)} runtimes**"]
    labels = {"claude_code": "Claude Code (sandboxed hands)",
              "openclaw": "OpenClaw (channels/reach)",
              "hermes": "Hermes (skills/recall)",
              "conductor": "Conductor (orchestration)"}
    for rt, names in by_rt.items():
        lines.append(f"\n{labels.get(rt, rt)} — {len(names)}:")
        for n in names:
            lines.append(f"  • {n}")
    return "\n".join(lines)
