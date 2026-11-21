"""ACP steering — drive a Claude Code session from a chat thread via OpenClaw.

The hybrid unlock (research doc 12 §d): OpenClaw's acpx can spawn/steer/cancel
Claude Code (and Codex/Gemini) over the Agent Client Protocol, so the owner can
kick off and steer a coding agent from his phone.

BUT (verified caveats): acpx is ALPHA, ACP runs HOST-SIDE and is NOT sandboxed
by OpenClaw, its bridge silently drops MCP config. So we treat ACP as a STEERING
WHEEL only, never a security boundary:
  * Spawning a coding agent is a state-changing action -> it goes through the
    broker as an approval, and the spawn is dispatched as an argv LIST with NO
    shell (see approval.action_queue._exec_acp_spawn) so chat input can't inject
    commands; the cwd is validated + confined to the home tree.
  * NOTE: the deny-guard hook only protects Bash calls INSIDE a `claude`
    subprocess; it does NOT cover the spawn command's own execution. That's why
    the spawn is no-shell + validated here rather than relying on the hook.
  * Once running, the Claude Code session it spawns has ITS OWN kernel sandbox +
    deny-guard, and the broker (not the agent) is still the only pusher.
  * This is optional and off unless OpenClaw + acpx are present; it never sits on
    the safety-critical path.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

logger = logging.getLogger("acp")


def available() -> bool:
    return shutil.which("openclaw") is not None


async def _acp(*args, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "openclaw", "acp", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, (out.decode(errors="ignore") + err.decode(errors="ignore"))
    except Exception as e:
        return 1, str(e)


import os
import re

_SAFE_CWD = re.compile(r"^[A-Za-z0-9._/\- ]+$")


def spawn_proposal(repo_cwd: str, task: str, model: str = "") -> dict:
    """Build a broker approval to spawn a Claude Code ACP session on a repo.

    SECURITY: the cwd comes from chat input, so it is validated and the spawn is
    dispatched as an argv LIST via a dedicated executor (no shell) — never a shell
    string (which would be command-injectable). Spawning a host-side coding agent
    is a mutation, so it's approval-gated regardless."""
    cwd = os.path.realpath(os.path.expanduser(repo_cwd))
    home = os.path.realpath(os.path.expanduser("~"))
    # confine to the home TREE (prefix + separator, so /Users/youEVIL can't
    # pass against home /Users/you) and reject any metacharacters
    in_home = cwd == home or cwd.startswith(home + os.sep)
    if not in_home or not _SAFE_CWD.match(cwd) or not os.path.isdir(cwd):
        return {
            "action_type": "send_notification",
            "description": f"Rejected ACP spawn — unsafe/nonexistent cwd: {repo_cwd[:60]}",
            "details": {}, "reasoning": "cwd failed validation (metachars / outside home / missing).",
            "project": "acp-coding", "tier": "R", "reversibility": "reversible",
            "criticality_hint": "routine",
        }
    argv = ["openclaw", "acp", "spawn", "claude", "--cwd", cwd]
    if model and re.match(r"^[A-Za-z0-9._/\-]+$", model):
        argv += ["--model", model]
    return {
        "action_type": "acp_spawn",
        "description": f"Spawn a Claude Code session on {cwd} to: {task[:80]}",
        "details": {"argv": argv, "cwd": cwd, "task": task[:500]},
        "reasoning": "ACP-steered coding session (phone-driven). Dispatched as an "
                     "argv list (no shell). Runs under Claude Code's own kernel "
                     "sandbox; the broker still owns any push/PR. Approve to start.",
        "project": "acp-coding", "tier": "A", "reversibility": "irreversible",
        # critical => holds for explicit approval, never auto-runs on a 30-min
        # timeout (spawning a host coding process should be a deliberate yes).
        "criticality_hint": "critical",
    }


async def steer(session_key: str, instruction: str) -> bool:
    """Send a steering instruction to a live ACP session."""
    if not available():
        return False
    rc, _ = await _acp("steer", instruction, "--session", session_key)
    return rc == 0


async def cancel(session_key: str) -> bool:
    if not available():
        return False
    rc, _ = await _acp("cancel", "--session", session_key)
    return rc == 0


def status_text() -> str:
    if not available():
        return "ACP steering needs OpenClaw (not installed)."
    return (
        "🎛️ ACP steering — drive a Claude Code coding session from chat.\n"
        "• `code on <owner/repo>: <task>` — I propose spawning a session (you ✅)\n"
        "• `steer: <instruction>` — nudge the live session\n"
        "• `cancel coding` — stop it\n\n"
        "The session runs under Claude Code's own sandbox + our deny-guard; any "
        "push/PR still goes through your approval. (ACP is alpha — treat as a "
        "steering wheel, not a security boundary.)")
