"""Self-update & self-acquire-tools — the army upgrades itself like a human does.

The owner: "self update, self acquire tools, like how human upgrades their skills and
memory." Two bounded, gated loops:

  1. SELF-UPDATE — detect outdated runtimes/deps (OpenClaw, Hermes, key MCP
     servers) and PROPOSE the upgrade as a critical approval with a one-command
     rollback. Never auto-upgrades the things on the safety-critical path
     (research doc 12: OpenClaw churn breaks ~25% of updates — so upgrades are a
     deliberate yes, staged, never silent).

  2. SELF-ACQUIRE — when a recurring need appears (from the failure-cluster /
     capability-gap signal), discover a candidate MCP server or skill via the
     existing vetted discovery pipeline (discovery/mcp_registry, skill_vetter),
     STATIC-SCAN it for the 1-in-5-malicious ClawHub risk, and PROPOSE it. New
     tools only land in the repo-local .claude/ after approval + vet pass.

Everything here is a proposal, never an autonomous mutation. The eval-gate +
human approval + git rollback are the guardrails (self-report never gates).
"""

from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger("self-update")


async def _ver(cmd: list[str], timeout: int = 20) -> str:
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
        return out.decode(errors="ignore").strip()
    except Exception:
        return ""


async def check_updates() -> list[dict]:
    """Return a list of available upgrades: {name, current, latest, upgrade_cmd}."""
    import re
    updates = []
    # OpenClaw — compare the ACTUAL installed version to the true npm-latest, and
    # upgrade with `install @latest` (NOT `npm update`, which is semver-bounded and
    # only reached 2026.5.12 when 2026.7.1 was available — the v0.11 bug).
    if shutil.which("openclaw"):
        vout = await _ver(["openclaw", "--version"], timeout=15)
        m = re.search(r"(\d+\.\d+\.\d+)", vout)
        installed = m.group(1) if m else ""
        latest = (await _ver(["npm", "view", "openclaw", "version"], timeout=20)).strip()
        if latest and installed and latest != installed:
            updates.append({"name": "openclaw", "current": installed,
                            "latest": latest,
                            "upgrade_cmd": "npm install -g openclaw@latest",
                            "risk": "medium (churn: ~25%/update breaks delivery — stage it)"})
    # Hermes — same true-latest comparison + @latest install
    if shutil.which("hermes"):
        vout = await _ver(["hermes", "--version"], timeout=15)
        m = re.search(r"(\d+\.\d+\.\d+)", vout)
        installed = m.group(1) if m else vout[:30]
        latest = (await _ver(["npm", "view", "hermes-agent", "version"], timeout=20)).strip()
        if latest and installed and latest not in installed:
            updates.append({"name": "hermes-agent", "current": installed,
                            "latest": latest,
                            "upgrade_cmd": "npm install -g hermes-agent@latest",
                            "risk": "medium (young project — read release notes)"})
    return updates


async def propose_updates(broker) -> dict:
    """Propose each available upgrade as a CRITICAL approval (never auto-run)."""
    updates = await check_updates()
    if not updates:
        return {"status": "up_to_date"}
    proposed = 0
    for u in updates:
        try:
            await broker.propose(
                action_type="run_command",
                description=f"Upgrade {u['name']} -> {u['latest']}",
                details={"command": u["upgrade_cmd"], "timeout": 300},
                reasoning=f"Newer {u['name']} available (current {u['current']}). "
                          f"Risk: {u['risk']}. One-command rollback via npm. Approve to apply.",
                project="sentinel-self", tier="A", reversibility="reversible",
                criticality_hint="critical")
            proposed += 1
        except Exception as e:
            logger.warning("update proposal failed for %s: %s", u["name"], e)
    return {"status": "proposed", "count": proposed,
            "updates": [u["name"] for u in updates]}


async def acquire_tool(broker, need: str) -> dict:
    """A capability gap was detected -> discover + vet + propose a new tool.

    Uses the existing vetted discovery pipeline. The tool is only added after
    approval AND a clean static-scan (the ClawHub 1-in-5-malicious guard)."""
    try:
        from discovery import mcp_registry
        candidates = mcp_registry.discover(need)
    except Exception as e:
        logger.debug("discovery failed: %s", e)
        candidates = []
    if not candidates:
        return {"status": "no_candidate", "need": need}
    # vet_and_propose already static-scans + gates the best candidate
    try:
        best = candidates[0]
        res = mcp_registry.vet_and_propose(broker, best if isinstance(best, dict) else {"name": str(best)})
        return {"status": "proposed", "need": need, "candidate": best.get("name") if isinstance(best, dict) else str(best),
                "vet": res}
    except Exception as e:
        logger.warning("acquire_tool propose failed: %s", e)
        return {"status": "error", "need": need, "error": str(e)[:120]}


def status_text() -> str:
    return ("🔄 Self-upgrade: I check OpenClaw/Hermes/tool versions and PROPOSE "
            "upgrades (critical approval, rollback-able) — never silent, because "
            "OpenClaw updates break ~1 in 4. Self-acquire: when a capability gap "
            "recurs, I discover + malware-scan + propose a new MCP/skill; it only "
            "installs into the repo-local .claude/ after your ✅. Everything is "
            "gated — I upgrade my skills like you do, but you sign off.")
