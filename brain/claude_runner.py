"""Claude Code CLI runner -- spawns claude as subprocess with streaming JSON output.

Hardened against known Claude Code CLI issues:
  - stream-json intermittent hang (github.com/anthropics/claude-code/issues/39700)
  - Post-result hang (github.com/anthropics/claude-code/issues/25629)
  - Orphan subprocess accumulation (github.com/anthropics/claude-code/issues/25180)

Mitigations:
  1. Process group isolation (os.setpgrp) for tree-wide kill
  2. Stale-output detection (configurable idle timeout)
  3. Result-event fast-exit (don't wait forever after result)
  4. Concurrent stderr draining
  5. Graceful kill cascade: SIGTERM → wait → SIGKILL on process group
  6. Global process registry for server-shutdown cleanup
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import logging
import atexit
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator

logger = logging.getLogger("claude-runner")

IDLE_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_IDLE_TIMEOUT", "300"))
POST_RESULT_WAIT_SECONDS = 5
KILL_GRACE_SECONDS = 5

# Signatures of a run whose "result" is actually a failure notice. Matched
# only against SHORT results (<400 chars) so a real analysis that merely
# discusses errors can't be misflagged.
_AUTH_ERR_RE = re.compile(
    r"(?:401 Unauthorized|Failed to authenticate|Invalid token"
    r"|OAuth token has expired|authentication_error|credit balance is too low"
    r"|Usage Policy)", re.I)

_active_processes: dict[int, asyncio.subprocess.Process] = {}

_CLAUDE_BIN_CACHE: str | None = None


def _claude_bin() -> str:
    """Absolute path to the `claude` CLI. Under launchd the daemon's PATH does
    NOT include ~/.local/bin (where the CLI installs + auto-updates), so a bare
    'claude' fails to spawn — the silent-chat root cause, 2026-08-04. Resolve it
    once by absolute path, checking the known install locations, then PATH."""
    global _CLAUDE_BIN_CACHE
    if _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE
    import shutil
    candidates = [
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    for c in candidates:
        if os.path.exists(c):
            _CLAUDE_BIN_CACHE = c
            return c
    found = shutil.which("claude")
    _CLAUDE_BIN_CACHE = found or "claude"    # last resort: bare name (may fail loudly)
    return _CLAUDE_BIN_CACHE


def _cleanup_all_processes():
    """Kill all tracked processes on server shutdown."""
    for pid, proc in list(_active_processes.items()):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    _active_processes.clear()


atexit.register(_cleanup_all_processes)


@dataclass
class ClaudeEvent:
    """A single event from Claude Code's stream-json output."""
    type: str  # "assistant", "tool_use", "tool_result", "error", "system", "result"
    content: str = ""
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    agent: str = ""
    model: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    claude_session_id: str = ""
    raw: dict = field(default_factory=dict)
    # True when the CLI reported the run as failed (is_error in the result
    # JSON) or the result text is an auth/error signature. Callers MUST treat
    # such a result as a failure, never as content — the Aug-12→14 outage
    # stored raw 401 strings as "meeting analysis" and logged success=True.
    is_error: bool = False


def _record_cost(agent: str, task_type: str | None, cost: float,
                 tin: int, tout: int) -> None:
    """B3: aggregate spend per day so the daemon can enforce a budget and so
    cost is no longer discarded at every call site. Best-effort; never raises."""
    try:
        from conductor import kv
        from datetime import datetime
        today = datetime.now().date().isoformat()
        led = kv.get("cost_ledger", {}) or {}
        if led.get("date") != today:
            led = {"date": today, "cost_usd": 0.0, "in": 0, "out": 0, "runs": 0, "by_agent": {}}
        led["cost_usd"] = round(led.get("cost_usd", 0.0) + (cost or 0.0), 5)
        led["in"] = led.get("in", 0) + (tin or 0)
        led["out"] = led.get("out", 0) + (tout or 0)
        led["runs"] = led.get("runs", 0) + 1
        led["by_agent"][agent] = round(led["by_agent"].get(agent, 0.0) + (cost or 0.0), 5)
        kv.put("cost_ledger", led)
    except Exception:
        pass


@dataclass
class ClaudeSession:
    """Tracks a Claude Code session for resume capability."""
    session_id: str = ""
    agent: str = "commander"
    model: str = ""
    is_running: bool = False
    process: asyncio.subprocess.Process | None = None


async def run_claude(
    prompt: str,
    agent: str = "conductor",
    model: str | None = None,
    session_id: str | None = None,
    working_dir: str | None = None,
    max_turns: int = 0,
    tier: str = "act",
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    task_type: str | None = None,
    episode_source: str = "run_claude",
) -> AsyncGenerator[ClaudeEvent, None]:
    """Spawn claude CLI and yield streaming events.

    Uses process groups for clean tree-wide termination, idle timeout
    to catch the known stream-json hang bug, and fast-exit after the
    result event to avoid the post-result hang.

    Security: does NOT use --dangerously-skip-permissions. Instead a per-tier
    permission profile is applied:
      tier="read"  -> read-only tools only (perception; Rule-of-Two: no secrets)
      tier="act"   -> read + edit + scoped bash (execution, within working_dir)
    Callers may override with permission_mode / allowed_tools.

    `tier` is the PERMISSION profile (read|act). `task_type` (e.g. coding/
    research/chat/analysis) drives EVIDENCE-BASED MODEL SELECTION: when the
    caller passes model=None, we resolve it from the model arena's proven
    winner, then the router — so the win-rate evidence the arena collects
    actually routes work (v0.12; it was collected but never read).
    """
    from config.paths import REPO_ROOT
    work_dir = working_dir or str(REPO_ROOT)

    # B1: evidence-based model resolution (only when the caller didn't pin one).
    # cli_only=True: only Anthropic winners — passing a proven Gemini/Grok/GPT
    # slug to `claude --model` would make the CLI reject it and the run fail.
    if model is None and task_type:
        try:
            from brain import arena
            model = arena.best_model(task_type, cli_only=True)
        except Exception:
            model = None
        if model is None:
            try:
                from brain import model_router
                model = model_router.select_model(task_type, for_cli=True)
            except Exception:
                model = None

    # Coaching tap: keep the clean pre-injection prompt so the episode record
    # holds the real ask, not the Learned-Rules prefix (the interaction_log
    # pollution the coach was built to fix).
    _clean_prompt = prompt
    _run_started = time.monotonic()

    try:
        from learning.skillbook import build_prompt_injection
        from learning.error_journal import get_warnings_for_task
        learned_rules = build_prompt_injection(agent)
        warnings = get_warnings_for_task(prompt, agent)
        prefix_parts = []
        # B2: inject the unified memory (corrections + preferences + facts) into
        # EVERY agent, not just the chat path — so all specialists inherit
        # The owner's learned preferences and never-repeat corrections.
        try:
            from memory import gateway
            mem = gateway.prompt_block(context_hint=prompt[:400], budget_chars=1200)
            if mem:
                prefix_parts.append(mem)
        except Exception:
            pass
        if learned_rules:
            prefix_parts.append(learned_rules)
        if warnings:
            prefix_parts.append(
                "## Warnings from past errors\n\n" + "\n".join(warnings)
            )
        if prefix_parts:
            prompt = "\n\n".join(prefix_parts) + "\n\n---\n\n" + prompt
    except Exception:
        pass

    slug_map = _AGENT_SLUG_MAP = {
        "codebot": "code-bot",
        "code_bot": "code-bot",
        "reviewbot": "review-bot",
        "review_bot": "review-bot",
        "workspace_assistant": "workspace-assistant",
        "github_autopilot": "code-bot",   # github vertical uses the coding agent
        "inbox_triage": "comms-drafter",  # drafting is the comms specialist's job
        # v0.9 specialist roster (files in .claude/agents/)
        "comms_drafter": "comms-drafter",
        "ops_watcher": "ops-watcher",
        "research": "researcher",
        "research_agent": "researcher",
        "meeting_intel": "workspace-assistant",  # meeting-intel vertical uses the read-only Calendar/Gmail/Drive specialist
    }
    agent_slug = slug_map.get(agent.lower().replace("-", "_"), agent)

    # Route through the TrueFoundry gateway if configured. Optional — the fresh
    # build usually inherits the CLI's own ~/.claude/settings.json auth, so this
    # is a best-effort import that no-ops if the module isn't present.
    try:
        from brain import truefoundry as _tfy  # optional, may not exist
        _tfy_enabled = _tfy.is_enabled()
    except Exception:
        _tfy = None
        _tfy_enabled = False

    if _tfy_enabled and model:
        model = _tfy.map_model(model)

    cmd = [
        _claude_bin(),
        "--verbose",
        "-p", prompt,
        "--output-format", "stream-json",
    ]

    # SECURITY + AUTH: always pass --settings so the deny-guard hook travels with
    # the agent regardless of cwd, AND so we never use --bare. --bare refuses
    # OAuth/keychain auth (only reads ANTHROPIC_API_KEY), which broke read-tier
    # runs on OAuth-logged-in machines (gap P0-2). By dropping --bare, read AND
    # act tiers inherit the CLI's normal auth (OAuth/keychain/TFY). Read-tier
    # isolation comes from the restricted --allowedTools + plan mode below, not
    # from --bare. The structural push/PR block is the deny-guard hook + the
    # broker being the only pusher.
    from config.paths import REPO_ROOT as _SENTINEL_ROOT
    settings_path = _SENTINEL_ROOT / ".claude" / "settings.json"
    if settings_path.exists():
        cmd.extend(["--settings", str(settings_path)])

    # Coding MCP servers (context7 live docs, github structured context, fetch)
    # — act tier only. Read/perception stays lean: research shows more tools
    # measurably degrade reasoning + cost, so we don't load MCP there.
    mcp_path = _SENTINEL_ROOT / ".mcp.json"
    if tier == "act" and mcp_path.exists() and os.getenv("SENTINEL_MCP", "1") != "0":
        cmd.extend(["--mcp-config", str(mcp_path)])

    # Agent definitions live in the repo-local .claude/agents; only pass --agent
    # if a slug was explicitly requested (the plain conductor has no agent file).
    if agent_slug and agent_slug not in ("conductor", "default"):
        cmd.extend(["--agent", agent_slug])

    if model:
        cmd.extend(["--model", model])

    if session_id:
        cmd.extend(["--resume", session_id])

    if max_turns > 0:
        cmd.extend(["--max-turns", str(max_turns)])

    # Per-tier permission profile — NO --dangerously-skip-permissions.
    # Read tier: read-only tools, no writes/bash. Act tier: acceptEdits with a
    # scoped allowed-tools list (bash is gated by settings.json PreToolUse hook +
    # the DANGEROUS_COMMANDS denylist in tools/system_tools.py).
    _READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]
    _ACT_TOOLS = _READ_TOOLS + ["Edit", "Write", "Bash"]
    tools = allowed_tools if allowed_tools is not None else (
        _READ_TOOLS if tier == "read" else _ACT_TOOLS
    )
    if tools:
        cmd.extend(["--allowedTools", ",".join(tools)])
    mode = permission_mode or ("plan" if tier == "read" else "acceptEdits")
    cmd.extend(["--permission-mode", mode])

    logger.info(
        "Spawning claude: agent=%s, model=%s, session=%s, max_turns=%d",
        agent, model or "default", session_id or "new", max_turns,
    )

    process: asyncio.subprocess.Process | None = None

    try:
        cli_env = {**os.environ, "BASH_MAX_OUTPUT_LENGTH": "100000"}
        # AUTH UNDER LAUNCHD: when the daemon runs as a launchd agent, os.environ
        # has almost nothing (launchd passes only PATH/PYTHONPATH) — so the gateway
        # credentials that live in ~/.claude/settings.json's `env` block are
        # ABSENT, and every spawned `claude` fails instantly with an empty result
        # (root cause of chat going silent for days, 2026-08-04). The CLI does NOT
        # reliably re-read that env block when spawned non-interactively, so load
        # it here and inject any gateway/auth vars os.environ is missing. Never
        # override a value the process already has (interactive shell wins).
        try:
            import json as _json
            _user_settings = Path.home() / ".claude" / "settings.json"
            if _user_settings.exists():
                _env_block = (_json.loads(_user_settings.read_text()) or {}).get("env", {})
                for _k, _v in (_env_block or {}).items():
                    if (_k.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "TFY_"))
                            and _k not in cli_env and _v):
                        cli_env[_k] = str(_v)
        except Exception as _e:
            logger.warning("could not load gateway auth from ~/.claude/settings.json: %s", _e)
        # AUTH (RCA 2026-08-14): settings.json no longer carries a static token
        # (auth moved to apiKeyHelper) — and the helper script can fail in a
        # launchd env. If the child env has no token, resolve one ourselves via
        # gateway_auth (keychain PAT → key file → auto-refreshed JWT) so a spawn
        # never depends on the shell helper working.
        if not (cli_env.get("ANTHROPIC_AUTH_TOKEN") or cli_env.get("ANTHROPIC_API_KEY")):
            try:
                from brain import gateway_auth
                _tok = gateway_auth.gateway_token()
                if _tok:
                    cli_env["ANTHROPIC_AUTH_TOKEN"] = _tok
            except Exception as _e:
                logger.warning("gateway token resolution failed: %s", _e)
        # SECURITY (gap P1-7): the coding agent runs attacker-influenced content
        # (issue bodies, repo code) and has Bash — it must NOT see credentials.
        # The broker holds GITHUB_TOKEN and pushes on the agent's behalf, so scrub
        # secrets from the child env. Keep ANTHROPIC/TFY auth (the CLI needs it).
        _SECRET_ENV = ("GITHUB_TOKEN", "SENTINEL_GITHUB_TOKEN", "BRAINTRUST_API_KEY",
                       "OPENAI_API_KEY", "GEMINI_API_KEY", "SENTINEL_TELEGRAM_TOKEN",
                       "SENTINEL_TELEGRAM_CHAT_ID", "AWS_SECRET_ACCESS_KEY",
                       "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN")
        for k in _SECRET_ENV:
            cli_env.pop(k, None)
        # MCP servers need their own keys via ${VAR} expansion in .mcp.json.
        # Inject keychain-backed MCP keys (act tier only, where MCP loads).
        # These are doc-fetch/read-only keys, not exfil-sensitive like the GitHub token.
        if tier == "act":
            for _mcp_key in ("CONTEXT7_API_KEY",):
                val = os.getenv(_mcp_key, "")
                if not val:
                    try:
                        import subprocess as _sp
                        val = _sp.run(["security", "find-generic-password", "-s",
                                       _mcp_key, "-w"], capture_output=True,
                                      text=True, check=True).stdout.strip()
                    except Exception:
                        val = ""
                if val:
                    cli_env[_mcp_key] = val
        # Observability: opt into Claude Code's OpenTelemetry so bad runs are
        # debuggable. Enabled by default; set SENTINEL_TELEMETRY=0 to disable.
        if os.getenv("SENTINEL_TELEMETRY", "1") != "0":
            cli_env.setdefault("CLAUDE_CODE_ENABLE_TELEMETRY", "1")
            cli_env.setdefault("OTEL_LOG_TOOL_DETAILS", "1")
        if _tfy_enabled:
            # Inject gateway config so Claude Code authenticates against
            # TrueFoundry, not Anthropic directly. In bearer mode this sets the
            # gateway token as ANTHROPIC_API_KEY (what `--bare` reads); in
            # header/max mode no API key is set, so drop any direct one.
            tfy_env = _tfy.claude_code_env()
            cli_env.update(tfy_env)
            if "ANTHROPIC_API_KEY" not in tfy_env:
                cli_env.pop("ANTHROPIC_API_KEY", None)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=cli_env,
            preexec_fn=os.setpgrp,
        )

        pgid = process.pid
        _active_processes[pgid] = process
        logger.info("Claude process started: pid=%d, pgid=%d", process.pid, pgid)

        stderr_task = asyncio.create_task(_drain_stderr(process, pgid))

        got_result = False

        try:
            async for line in _read_lines_with_timeout(process, IDLE_TIMEOUT_SECONDS):
                if line is None:
                    logger.warning(
                        "Claude idle for %ds (pid=%d) -- killing stale process",
                        IDLE_TIMEOUT_SECONDS, pgid,
                    )
                    yield ClaudeEvent(
                        type="error",
                        content=f"Claude CLI stopped producing output for {IDLE_TIMEOUT_SECONDS}s and was terminated.",
                    )
                    await _kill_process_group(pgid, process)
                    break

                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    yield ClaudeEvent(type="system", content=line)
                    continue

                for event in _parse_events(data):
                    yield event
                    if event.type == "result":
                        got_result = True
                        if event.is_error:
                            # Loud, greppable, and self-healing: a failed run
                            # must never masquerade as output (Aug-12→14 RCA),
                            # and an auth-shaped failure drops the cached token
                            # so the NEXT spawn re-resolves a fresh one.
                            logger.error("Claude run FAILED (agent=%s): %s",
                                         agent, (event.content or "")[:250])
                            if _AUTH_ERR_RE.search(event.content or ""):
                                try:
                                    from brain import gateway_auth
                                    gateway_auth.invalidate()
                                except Exception:
                                    pass
                        _record_cost(agent, task_type, event.cost_usd,
                                     event.input_tokens, event.output_tokens)
                        # Coaching tap: persist the faithful episode (clean
                        # prompt + final output + session id). Best-effort.
                        try:
                            from coaching.episodes import record_episode
                            _raw = event.raw if isinstance(event.raw, dict) else {}
                            record_episode(
                                agent=agent, prompt=_clean_prompt,
                                result=event.content or "",
                                tier=tier, model=event.model or (model or ""),
                                task_type=task_type or "",
                                success=(not _raw.get("is_error", False)),
                                duration_ms=int((time.monotonic() - _run_started) * 1000),
                                cost_usd=event.cost_usd,
                                session_id=event.claude_session_id,
                                cwd=work_dir,
                                injected_chars=max(0, len(prompt) - len(_clean_prompt)),
                                source=episode_source,
                            )
                        except Exception:
                            pass

                if got_result:
                    break

        except asyncio.CancelledError:
            logger.info("Claude task cancelled (pid=%d)", pgid)
            await _kill_process_group(pgid, process)
            raise

        if got_result:
            try:
                await asyncio.wait_for(process.wait(), timeout=POST_RESULT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    "Claude post-result hang detected (pid=%d) -- killing",
                    pgid,
                )
                await _kill_process_group(pgid, process)
        else:
            await process.wait()

        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

    except FileNotFoundError:
        yield ClaudeEvent(
            type="error",
            content="Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
        )
    except Exception as e:
        yield ClaudeEvent(type="error", content=f"Claude runner error: {str(e)}")
    finally:
        if process and process.pid:
            _active_processes.pop(process.pid, None)
            if process.returncode is None:
                await _kill_process_group(process.pid, process)


async def _read_lines_with_timeout(
    process: asyncio.subprocess.Process,
    idle_timeout: int,
) -> AsyncGenerator[str | None, None]:
    """Read lines from stdout, yielding None if idle_timeout is exceeded."""
    while True:
        try:
            line_bytes = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=idle_timeout,
            )
        except asyncio.TimeoutError:
            yield None
            return

        if not line_bytes:
            return

        yield line_bytes.decode("utf-8", errors="replace").strip()


async def _drain_stderr(process: asyncio.subprocess.Process, pgid: int):
    """Continuously drain stderr in the background."""
    try:
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace").strip()
            if text:
                for line in text.split("\n"):
                    line = line.strip()
                    if line:
                        logger.debug("Claude stderr (pid=%d): %s", pgid, line[:300])
    except (asyncio.CancelledError, Exception):
        pass


async def _kill_process_group(pgid: int, process: asyncio.subprocess.Process):
    """Graceful kill cascade: SIGTERM the group → wait → SIGKILL."""
    try:
        os.killpg(pgid, signal.SIGTERM)
        logger.info("Sent SIGTERM to process group %d", pgid)
    except (ProcessLookupError, PermissionError, OSError):
        return

    try:
        await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
        logger.info("Process group %d exited after SIGTERM", pgid)
        return
    except asyncio.TimeoutError:
        pass

    try:
        os.killpg(pgid, signal.SIGKILL)
        logger.warning("Sent SIGKILL to process group %d", pgid)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        logger.error("Process group %d did not exit after SIGKILL", pgid)


def _parse_events(data: dict) -> list[ClaudeEvent]:
    """Parse a verbose stream-json line into one or more ClaudeEvents."""
    event_type = data.get("type", "unknown")

    if event_type == "assistant":
        events: list[ClaudeEvent] = []
        msg = data.get("message", {})
        model = msg.get("model", "") if isinstance(msg, dict) else ""
        content_blocks = msg.get("content", []) if isinstance(msg, dict) else []

        if isinstance(msg, str):
            return [ClaudeEvent(type="assistant", content=msg, raw=data)]

        for block in content_blocks:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type", "")

            if block_type == "text":
                events.append(ClaudeEvent(
                    type="assistant",
                    content=block.get("text", ""),
                    model=model,
                    raw=data,
                ))
            elif block_type == "tool_use":
                events.append(ClaudeEvent(
                    type="tool_use",
                    tool_name=block.get("name", ""),
                    tool_input=block.get("input", {}),
                    model=model,
                    raw=data,
                ))

        return events if events else [ClaudeEvent(type="system", content="", raw=data)]

    elif event_type == "user":
        content_blocks = data.get("message", {}).get("content", [])
        for block in content_blocks if isinstance(content_blocks, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return [ClaudeEvent(
                    type="tool_result",
                    content=str(block.get("content", ""))[:500],
                    raw=data,
                )]
        return []

    elif event_type == "tool_use":
        return [ClaudeEvent(
            type="tool_use",
            tool_name=data.get("tool", data.get("name", "")),
            tool_input=data.get("input", {}),
            content=data.get("content", ""),
            raw=data,
        )]

    elif event_type == "result":
        _content = str(data.get("result", data.get("output", data.get("content", ""))))
        _is_err = bool(data.get("is_error", False))
        # Belt-and-braces: a short result that IS an auth-failure string is an
        # error even if the CLI didn't flag it (observed shape of the Aug-12
        # outage: "Failed to authenticate. API Error: 401 Unauthorized ...").
        if not _is_err and len(_content) < 400 and _AUTH_ERR_RE.search(_content):
            _is_err = True
        return [ClaudeEvent(
            type="result",
            content=_content,
            cost_usd=data.get("total_cost_usd", data.get("cost_usd", 0.0)),
            input_tokens=data.get("usage", {}).get("input_tokens", 0),
            output_tokens=data.get("usage", {}).get("output_tokens", 0),
            claude_session_id=data.get("session_id", ""),
            raw=data,
            is_error=_is_err,
        )]

    elif event_type == "error":
        return [ClaudeEvent(
            type="error",
            content=data.get("error", data.get("message", data.get("content", str(data)))),
            raw=data,
        )]

    elif event_type == "system":
        return []

    else:
        return [ClaudeEvent(
            type=event_type,
            content=json.dumps(data) if isinstance(data, dict) else str(data),
            raw=data,
        )]


async def check_claude_installed() -> bool:
    """Check if claude CLI is available."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return False
        return proc.returncode == 0
    except FileNotFoundError:
        return False


def get_active_process_count() -> int:
    """Return the number of currently tracked Claude processes."""
    return len(_active_processes)


async def kill_all_active():
    """Kill all active Claude processes. Used during server shutdown."""
    for pgid, proc in list(_active_processes.items()):
        await _kill_process_group(pgid, proc)
    _active_processes.clear()
