"""Hermes adapter — AUTONOMOUS memory/skill enrichment, safely bounded.

The owner: "user won't be deciding whether to use Hermes — it should just be
autonomous." So this no longer needs a manual opt-in env: if the `hermes` binary
is present, the system uses it automatically for what it's genuinely best at
(research doc 13): SKILL GENERATION and cross-session memory recall — but never
as the memory hub (the unified gateway is canonical) and never with host-danger:

  * SAFETY INVARIANT — every call passes `-t <read-only toolset>` (memory or
    skills ONLY). That is the real control: it excludes Hermes's dangerous
    toolsets (terminal, file, code_execution, computer_use) from the run. We
    NEVER pass `--yolo`. (Note: `--safe-mode` is a *troubleshooting* flag that
    disables customizations + memory injection — it is NOT a sandbox, so we do
    NOT rely on it, and we must not use it on recall since it would suppress the
    very memory recall reads.) The `_hermes(...)` helper enforces the `-t`
    allow-list so no call site can widen it by accident.
  * Its self-generated skills are UNTRUSTED DRAFTS — they flow through the same
    eval-gate + human approval as any self-change before the army uses them.
  * Its memory is READ into the canonical gateway by memory/sync.py; we never
    make Hermes the shared write hub (single-writer, lock-race risk).
  * `-Q` (quiet) gives the final response only, so we don't scrape the TUI frame.
  * Every call is bounded + degrades to a no-op if Hermes is absent/errors, so
    nothing on the critical path depends on it.

Verified against Hermes Agent v0.18.2 CLI (subcommands: memory, skills, chat).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

logger = logging.getLogger("hermes")

# Autonomous: on when the binary exists. A hard kill-switch remains via env for
# emergencies (SENTINEL_HERMES=off), but the default is USE-IF-AVAILABLE.
import os
_KILL = os.getenv("SENTINEL_HERMES", "").lower() in ("off", "0", "false", "no")

# The ONLY toolsets we ever allow Hermes to load — read-only, no shell/file/exec.
# This allow-list IS the safety boundary (not --safe-mode).
_ALLOWED_TOOLSETS = {"memory", "skills"}


def _bin() -> str | None:
    return shutil.which("hermes") or shutil.which("hermes-agent")


def available() -> bool:
    return (not _KILL) and _bin() is not None


def status() -> dict:
    # NB: Hermes runs as a NATIVE subprocess contained by the read-only toolset
    # allow-list — NOT Docker-isolated. No `docker` field: it was never gated on.
    return {"binary": bool(_bin()), "kill_switch": _KILL, "usable": available()}


def _child_env() -> dict:
    """Inject model-provider credentials into Hermes's env at spawn time only —
    never written to Hermes's config on disk (same safe pattern as
    claude_runner).

    PRIMARY (2026-08-14, the owner): Hermes runs on the TrueFoundry gateway via
    its OpenAI-compatible surface — OPENAI_BASE_URL=<tfy> + the tfy_ token as
    OPENAI_API_KEY, model `openai/claude-group/claude-sonnet-5` (see
    .cursor/skills/truefoundry-gateway). Gemini key kept as fallback."""
    env = dict(os.environ)
    try:
        from brain import gateway_auth
        tok = gateway_auth.gateway_token()
        base = gateway_auth.base_url()
        if tok and base:
            env["OPENAI_API_KEY"] = tok
            # hermes's `custom` provider reads CUSTOM_BASE_URL (it stopped
            # consulting OPENAI_BASE_URL — runtime_provider.py:1091); keep
            # OPENAI_BASE_URL too for its auxiliary-client env fallback.
            env["CUSTOM_BASE_URL"] = base
            env["OPENAI_BASE_URL"] = base
            # hermes host-gates credentials (issue #28660): OPENAI_API_KEY is
            # only sent to openai.com hosts; other custom endpoints get
            # `<VENDOR>_API_KEY` derived from the registrable domain —
            # llm-gateway.example.com → GATEWAY_API_KEY.
            env["GATEWAY_API_KEY"] = tok
    except Exception:
        pass
    try:
        from brain import providers
        gk = providers.resolve_key("GEMINI_API_KEY")
        if gk:
            env["GEMINI_API_KEY"] = gk
            env.setdefault("GOOGLE_API_KEY", gk)
    except Exception:
        pass
    return env


import re as _re

_BOX = _re.compile(r"[╭╮╰╯│─┌┐└┘├┤┬┴┼]")


def _clean(out: str) -> str:
    """Strip Hermes's TUI frame + the echoed query/init lines, keep the answer."""
    lines = []
    for l in out.splitlines():
        s = _BOX.sub("", l)
        # drop the '⚕ Hermes' header, init/session/resume chrome, and the query
        # echo. NARROW patterns only: a bare `Hermes\b` filter here silently
        # deleted every CONTENT line mentioning Hermes — which emptied the
        # orchestrator's whole answer to "are you Claude or Hermes?"
        # (found live 2026-08-14; the '-Q returns nothing' mystery was this).
        if _re.search(r"^\s*⚕|Hermes Agent v\d|^\s*hermes --resume|Initializing agent|"
                      r"Resume this session|^\s*Session:|Duration:|Messages:|tirith|"
                      r"^\s*Query:|Output only the markdown", s):
            continue
        if s.strip():
            lines.append(s.rstrip())
    return "\n".join(lines).strip()


async def _run(*args, timeout: int = 45, input_text: str = "") -> tuple[int, str]:
    if not available():
        return 127, "hermes unavailable"
    try:
        proc = await asyncio.create_subprocess_exec(
            _bin(), *args, env=_child_env(),
            stdin=asyncio.subprocess.PIPE if input_text else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text else None), timeout=timeout)
        raw = out.decode(errors="ignore") or err.decode(errors="ignore")
        return proc.returncode, _clean(raw)
    except Exception as e:
        logger.debug("hermes call failed: %s", e)
        return 1, str(e)


async def _hermes(toolset: str, prompt: str, timeout: int = 45) -> tuple[int, str]:
    """The ONLY way this module invokes Hermes. Enforces the read-only toolset
    allow-list (the real safety boundary), quiet output (-Q, so no TUI scraping),
    never --yolo, never --safe-mode. Any toolset outside _ALLOWED_TOOLSETS is
    refused here so a call site can't widen the surface."""
    if toolset not in _ALLOWED_TOOLSETS:
        logger.warning("refused hermes call with disallowed toolset %r", toolset)
        return 1, ""
    return await _run("chat", "-Q", "-t", toolset, "-q", prompt, timeout=timeout)


async def recall(query: str, k: int = 5) -> list[str]:
    """Cross-session recall — enrichment only. The unified gateway is the real
    source; this adds Hermes's session-history hits when available.

    Uses `-t memory` (toolset allow-list = the safety bound). Does NOT use
    --safe-mode (which would disable the memory injection recall depends on).
    Returns [] on any failure — nothing depends on it."""
    if not available():
        return []
    prompt = (f"From your memory of past sessions only, list up to {k} facts or "
              f"prior outcomes relevant to: {query}. One per line, terse. If nothing "
              f"relevant, reply NONE.")
    rc, out = await _hermes("memory", prompt, timeout=40)
    if rc != 0 or "NONE" in out[:20].upper():
        return []
    lines = [l.strip("-• ").strip() for l in out.splitlines()
             if l.strip() and len(l.strip()) > 8][:k]
    return lines


async def draft_skill(task_pattern: str) -> dict | None:
    """Hermes's genuine edge: generate a reusable skill for a recurring pattern.
    Returned as an UNTRUSTED DRAFT — goes through eval-gate + approval before use."""
    if not available():
        return None
    prompt = (f"Draft a concise reusable agent skill (SKILL.md style: a name, a "
              f"one-line description, and 5-10 numbered steps) for this recurring "
              f"task pattern:\n{task_pattern}\nReturn only the skill markdown, no preamble.")
    rc, out = await _hermes("skills", prompt, timeout=60)
    if rc != 0 or len(out) < 40:
        return None
    return {"name": task_pattern[:40], "body": out.strip()[:2000],
            "source": "hermes-draft", "trusted": False}


def status_text() -> str:
    s = status()
    if not s["binary"]:
        return ("Hermes isn't installed. When present, I use it autonomously "
                "(safe-mode) for skill drafts + cross-session recall — as "
                "enrichment; the unified memory store stays canonical.")
    if s["kill_switch"]:
        return "Hermes is installed but kill-switched (SENTINEL_HERMES=off)."
    return ("🧠 Hermes active (autonomous). Used for skill generation + cross-"
            "session recall, restricted to read-only toolsets (memory/skills — "
            "no shell/file/exec), never --yolo. Its skills are untrusted drafts "
            "needing approval; its memory syncs into the ONE canonical store. "
            "It is not the memory hub — the Conductor's gateway is.")
