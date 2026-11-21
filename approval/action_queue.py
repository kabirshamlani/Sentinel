"""Action Queue -- propose, approve, reject, and execute agent actions.

Ported from agent-army with: storage paths from config.paths, executors
repointed to sentinel.tools.*, and extra fields consumed by approval.broker
(project, criticality, tier, reversibility, best_effort_plan, channel delivery
metadata, window_expires_at, resolved_by). The dedup + 7-day rejection memory
are preserved verbatim.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

from config.paths import (
    ACTION_QUEUE_PATH as QUEUE_PATH,
    COMPLETION_LOG_PATH,
    REJECTION_LOG_PATH,
)

logger = logging.getLogger("action-queue")

ActionType = Literal[
    "draft_email", "send_email", "draft_dm", "draft_gchat", "send_gchat",
    "create_task", "update_doc",
    "run_command", "create_branch", "create_pr", "create_issue", "update_issue", "schedule_meeting",
    "send_notification", "edit_file", "self_modify", "add_mcp_server",
    "add_skill", "create_agent", "aws_mutation",
]
ActionStatus = Literal["pending", "approved", "rejected", "executed", "failed"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"actions": [], "stats": {"total_proposed": 0, "approved": 0, "rejected": 0}}


def _save(data: dict):
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def propose_action(
    action_type: ActionType,
    description: str,
    details: dict[str, Any],
    reasoning: str,
    source: str = "conductor",
    *,
    project: str = "",
    criticality: str = "routine",
    tier: str = "A",
    reversibility: str = "reversible",
    best_effort_plan: str = "",
) -> dict:
    """Propose a new action for approval. Returns the action dict.

    Skips duplicates and previously-rejected actions (7-day memory).
    """
    # target = the concrete object dedup/reject key on. Include `command` so run_command
    # dedups on the actual command, not the constant "Run on your machine:" description
    # prefix (gap-2: rejecting one command was colliding with every short command).
    target = details.get("to", details.get("person_name",
                          details.get("command", details.get("title", ""))))
    if is_duplicate_action(action_type, description, target):
        logger.info("Skipping duplicate action: %s -- %s", action_type, description[:60])
        return {"id": "skipped", "status": "skipped", "reason": "duplicate_detected"}
    if is_rejected_action(action_type, description, target):
        logger.info("Skipping previously rejected action: %s -- %s", action_type, description[:60])
        return {"id": "skipped", "status": "skipped", "reason": "previously_rejected"}

    data = _load()
    for a in data["actions"]:
        if a.get("status") != "pending":
            continue
        if a.get("type") == action_type and a.get("description") == description:
            return a
        a_desc = (a.get("description") or "").lower()
        desc_lower = description.lower()
        if a.get("type") == action_type and len(desc_lower) > 10 and len(a_desc) > 10:
            overlap = set(a_desc.split()) & set(desc_lower.split())
            if len(overlap) >= max(3, min(len(a_desc.split()), len(desc_lower.split())) * 0.5):
                return a

    action = {
        "id": f"action-{uuid.uuid4().hex[:8]}",
        "type": action_type,
        "description": description,
        "details": details,
        "reasoning": reasoning,
        "source": source,
        "status": "pending",
        "proposed_at": _now(),
        "resolved_at": None,
        "execution_result": None,
        # broker fields
        "project": project,
        "criticality": criticality,          # routine | critical
        "tier": tier,                        # R | A | X
        "reversibility": reversibility,      # reversible | irreversible
        "best_effort_plan": best_effort_plan,
        "channels_sent": [],
        "delivery_msg_ids": {},
        "window_expires_at": None,
        "resolved_by": None,
    }
    data["actions"].append(action)
    data["stats"]["total_proposed"] = data["stats"].get("total_proposed", 0) + 1

    if len(data["actions"]) > 200:
        done = [a for a in data["actions"] if a["status"] in ("executed", "rejected", "failed")]
        if len(done) > 100:
            data["actions"] = [a for a in data["actions"] if a not in done[:50]]

    _save(data)
    logger.info("Proposed action: %s -- %s [%s/%s]", action_type, description[:50], criticality, tier)
    return action


def get_action(action_id: str) -> dict | None:
    for a in _load()["actions"]:
        if a["id"] == action_id:
            return a
    return None


def update_action(action_id: str, **fields) -> dict | None:
    data = _load()
    for a in data["actions"]:
        if a["id"] == action_id:
            a.update(fields)
            _save(data)
            return a
    return None


def approve_action(action_id: str, resolved_by: str = "user") -> dict | None:
    """Approve an action, triggering execution."""
    data = _load()
    for a in data["actions"]:
        if a["id"] == action_id and a["status"] == "pending":
            a["status"] = "approved"
            a["resolved_at"] = _now()
            a["resolved_by"] = resolved_by
            data["stats"]["approved"] = data["stats"].get("approved", 0) + 1
            _save(data)

            result = _execute(a)
            a["execution_result"] = result
            a["status"] = "executed" if not result.get("error") else "failed"
            _save(data)

            if a["status"] == "executed":
                target = a.get("details", {}).get("to", a.get("details", {}).get("person_name", ""))
                log_completion(a["type"], a["description"], target, a.get("details", {}).get("channel", ""))
            return a
    return None


def reject_action(action_id: str, reason: str = "", resolved_by: str = "user") -> dict | None:
    """Reject an action, logging it so similar actions aren't re-proposed for 7 days."""
    data = _load()
    for a in data["actions"]:
        if a["id"] == action_id and a["status"] == "pending":
            a["status"] = "rejected"
            a["resolved_at"] = _now()
            a["resolved_by"] = resolved_by
            a["rejection_reason"] = reason
            data["stats"]["rejected"] = data["stats"].get("rejected", 0) + 1
            _save(data)
            log_rejection(
                action_type=a.get("type", ""),
                description=a.get("description", ""),
                target=a.get("details", {}).get("to", a.get("details", {}).get("person_name", "")),
                reason=reason,
            )
            return a
    return None


def log_rejection(action_type: str, description: str, target: str, reason: str = ""):
    REJECTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"timestamp": _now(), "action_type": action_type,
             "description": description[:200], "target": target, "reason": reason}
    with open(REJECTION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_recent_rejections(hours: int = 168) -> list[dict]:
    if not REJECTION_LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    results = []
    for line in REJECTION_LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            ts = entry.get("timestamp", "")
            if ts:
                et = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if et >= cutoff:
                    results.append(entry)
        except (json.JSONDecodeError, ValueError):
            continue
    return results


def _strip_boilerplate(s: str) -> str:
    """Remove constant description prefixes so dedup/reject match on the real payload,
    not the shared prefix (gap-2: 'Run on your machine: X' vs '...: Y' collided)."""
    for pfx in ("run on your machine:", "send email to", "send gchat to",
                "draft reply to", "post to"):
        if s.startswith(pfx):
            return s[len(pfx):].strip()
    return s


def is_rejected_action(action_type: str, description: str, target: str) -> bool:
    rejections = get_recent_rejections(hours=168)
    desc_lower, target_lower = _strip_boilerplate(description.lower()), (target or "").lower()
    for r in rejections:
        if r.get("action_type") != action_type:      # only same-type rejections count
            continue
        r_desc = _strip_boilerplate(r.get("description", "").lower())
        r_target = r.get("target", "").lower()
        # same concrete target (recipient/command) rejected → skip
        if target_lower and r_target and (target_lower == r_target
                or target_lower in r_target or r_target in target_lower):
            return True
        # otherwise require STRONG description overlap on the de-boilerplated payload
        if len(desc_lower) > 10 and len(r_desc) > 10:
            a, b = set(desc_lower.split()), set(r_desc.split())
            overlap = a & b
            # need > 60% of the SHORTER payload's words to match (was a flat >3 on the
            # full description incl. the shared prefix — which false-matched everything)
            if overlap and len(overlap) >= 0.6 * min(len(a), len(b)) and len(overlap) >= 3:
                return True
    return False


def get_actions(status: str | None = None) -> list[dict]:
    data = _load()
    actions = data.get("actions", [])
    if status:
        actions = [a for a in actions if a.get("status") == status]
    return sorted(actions, key=lambda a: a.get("proposed_at", ""), reverse=True)


def get_pending_actions() -> list[dict]:
    return get_actions("pending")


def get_action_stats() -> dict:
    data = _load()
    stats = data.get("stats", {})
    total, approved, rejected = stats.get("total_proposed", 0), stats.get("approved", 0), stats.get("rejected", 0)
    return {"total_proposed": total, "approved": approved, "rejected": rejected,
            "approval_rate": approved / max(total, 1),
            "pending": len([a for a in data.get("actions", []) if a.get("status") == "pending"])}


def _execute(action: dict) -> dict:
    """Dispatch an approved action to the appropriate tool."""
    at, details = action.get("type", ""), action.get("details", {})
    try:
        if at == "draft_email":
            return _exec_draft_email(details)
        if at == "send_email":
            return _exec_send_email(details)
        if at == "create_branch":
            return _exec_create_branch(details)
        if at == "create_pr":
            return _exec_create_pr(details)
        if at == "create_issue":
            from tools import github_tools
            return github_tools.create_issue(
                details.get("owner", ""), details.get("repo", ""),
                details.get("title", ""), details.get("body", ""),
                details.get("labels") or [])
        if at == "update_issue":
            from tools import github_tools
            return github_tools.update_issue(
                details.get("owner", ""), details.get("repo", ""),
                int(details.get("number", 0)),
                state=details.get("state", ""),
                labels=details.get("labels"),
                body=details.get("new_body", ""))
        if at == "run_command":
            return _exec_run_command(details)
        if at == "edit_file":
            return _exec_edit_file(details)
        if at == "send_gchat":
            return _exec_send_gchat(details)
        if at == "acp_spawn":
            return _exec_acp_spawn(details)
        if at in ("draft_dm", "draft_gchat"):
            return _exec_draft_dm(details)
        if at == "schedule_meeting":
            return _exec_schedule_meeting(details)
        if at == "send_notification":
            return {"status": "notified"}
        # self_modify / add_mcp_server / add_skill / create_agent / aws_mutation
        # carry a pre-built shell command, or (create_agent/self_modify) a
        # {path, content} file write constrained to the repo's .claude/ dir.
        if at == "add_mcp_server":
            return _exec_add_mcp_server(details)
        if at in ("self_modify", "add_skill", "create_agent", "aws_mutation"):
            if details.get("command"):
                return _exec_run_command(details)
            # add_skill / create_agent / self_modify → write a path-locked .claude/*.md
            if at in ("add_skill", "create_agent", "self_modify") and \
                    (details.get("path") or details.get("agent_path")) and details.get("content"):
                return _exec_write_agent_file(details)
            return {"status": "recorded", "note": f"{at} approved; handler runs in vertical"}
        return {"error": f"Unknown action type: {at}"}
    except Exception as e:
        logger.error("Action execution failed: %s", e)
        return {"error": str(e)}


def log_completion(action_type: str, description: str, target: str = "", channel: str = ""):
    COMPLETION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"type": action_type, "description": description, "target": target,
             "channel": channel, "completed_at": _now()}
    with open(COMPLETION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_recent_completions(hours: int = 72) -> list[dict]:
    if not COMPLETION_LOG_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    results = []
    try:
        for line in COMPLETION_LOG_PATH.read_text(encoding="utf-8").strip().split("\n"):
            if not line:
                continue
            entry = json.loads(line)
            ts = entry.get("completed_at", "")
            if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff:
                results.append(entry)
    except Exception:
        pass
    return results


def is_duplicate_action(action_type: str, description: str, target: str = "") -> bool:
    desc_lower, target_lower = description.lower(), (target or "").lower()
    for c in get_recent_completions(72):
        if c.get("type") != action_type:
            continue
        c_target = str(c.get("target", "")).lower()
        c_desc = str(c.get("description", "")).lower()
        # gap-3 fix: same recipient ALONE is NOT a duplicate (a 2nd distinct email to the
        # same person is legitimate). Require the recipient match AND the same content, OR
        # an exact description match.
        target_match = bool(target_lower) and bool(c_target) and target_lower == c_target
        content_match = bool(desc_lower) and c_desc and (desc_lower == c_desc
                        or desc_lower in c_desc or c_desc in desc_lower)
        if (target_match and content_match) or (desc_lower and desc_lower == c_desc):
            return True
    # still-pending: an identical description is a genuine duplicate to skip
    for a in get_actions("pending"):
        if a.get("type") == action_type:
            a_desc = (a.get("description") or "").lower()
            if desc_lower and a_desc and desc_lower == a_desc:
                return True
    return False


# --- executors (repointed to sentinel.tools.*) ---

def _exec_draft_dm(details: dict) -> dict:
    person = details.get("person_name", details.get("to", ""))
    message = details.get("message", details.get("body", ""))
    if not message or not (person or details.get("space_id")):
        return {"error": "Missing person_name/space_id or message for DM"}
    # direct route: proposals now carry the space_id, no name lookup needed
    if details.get("space_id"):
        return {"status": "dm_ready", "person": person,
                "space_id": details["space_id"], "message": message}
    try:
        from tools.workspace_tools import list_dm_spaces
        dm = list_dm_spaces()
        if isinstance(dm, dict) and "dm_spaces" in dm:
            for sp in dm["dm_spaces"]:
                if person.lower() in str(sp.get("display_name", "")).lower():
                    return {"status": "dm_ready", "person": person,
                            "space_id": sp.get("space_id", ""), "message": message}
        return {"status": "dm_no_space", "person": person, "message": message,
                "fallback_action": "draft_email"}
    except Exception as e:
        return {"error": f"DM lookup failed: {e}", "person": person, "message": message}


def _run_async(coro):
    """Run an async gog helper from the sync executor path."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # already inside an event loop (daemon) — run in a fresh thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def _exec_draft_email(details: dict) -> dict:
    # ROOT-CAUSE GUARD: never create an empty/contentless draft. Old runs left 9 empty
    # drafts in the owner's Gmail (no to/subject/body) that persisted forever — "why am I
    # seeing this every time". A draft with no recipient AND no body is junk; refuse it.
    to = (details.get("to", "") or "").strip()
    subject = (details.get("subject", "") or "").strip()
    body = (details.get("body", "") or "").strip()
    if not body or (not to and not subject):
        return {"status": "skipped", "reason": "empty_draft_refused",
                "note": "no recipient/body — not creating a junk draft"}
    try:
        from tools.gog_workspace import create_draft
        r = _run_async(create_draft(to, subject, body))
        return {"status": "draft_created", **(r if isinstance(r, dict) else {"raw": str(r)})}
    except Exception as e:
        return {"error": f"Draft creation failed: {e}"}


_SENSITIVE_ATTACH = (".ssh", ".aws", ".gnupg", ".truefoundry", ".hermes",
                     ".claude", ".config", "Library/Keychains")
_SENSITIVE_NAMES = re.compile(r"(^\.env|credentials|id_rsa|id_ed25519|\.pem$"
                              r"|token|keychain|secrets?\.)", re.I)
_ATTACH_MAX_MB = 20


def _safe_attachments(paths: list) -> tuple[list[str], list[str]]:
    """(ok, rejected-with-reasons). An email attachment is an EGRESS of file
    contents — validate hard: must exist inside HOME, never from credential
    dirs or secret-shaped filenames, bounded size. Rejections are surfaced,
    never silently dropped (an email claiming an attachment it lacks is the
    exact bug this fixes)."""
    import os
    ok, rejected = [], []
    home = os.path.realpath(os.path.expanduser("~")) + os.sep
    for raw in paths or []:
        p = os.path.realpath(os.path.expanduser(str(raw)))
        name = os.path.basename(p)
        if not os.path.isfile(p):
            rejected.append(f"{raw}: not a file")
        elif not p.startswith(home):
            rejected.append(f"{raw}: outside home")
        elif any(os.sep + s + os.sep in p or p.endswith(os.sep + s) for s in _SENSITIVE_ATTACH):
            rejected.append(f"{raw}: sensitive directory")
        elif _SENSITIVE_NAMES.search(name):
            rejected.append(f"{raw}: secret-shaped filename")
        elif os.path.getsize(p) > _ATTACH_MAX_MB * 1024 * 1024:
            rejected.append(f"{raw}: larger than {_ATTACH_MAX_MB}MB")
        else:
            ok.append(p)
    return ok, rejected


def _exec_send_email(details: dict) -> dict:
    """Approved action only — actually sends via gog."""
    try:
        from tools.gog_workspace import send_email
        attachments, rejected = _safe_attachments(details.get("attachments") or [])
        if rejected:
            # honest failure: never send an email that silently lacks the
            # files it promises (the 2026-08-14 resume bug)
            return {"error": "attachment(s) rejected: " + "; ".join(rejected)}
        r = _run_async(send_email(details.get("to", ""), details.get("subject", ""),
                                  details.get("body", ""), attachments=attachments))
        if isinstance(r, dict) and r.get("error"):
            return r
        return {"status": "email_sent", "attached": len(attachments),
                **(r if isinstance(r, dict) else {"raw": str(r)})}
    except Exception as e:
        return {"error": f"Email send failed: {e}"}


def _exec_send_gchat(details: dict) -> dict:
    """Approved action only — actually sends a Google Chat message via gog."""
    try:
        from tools.gog_workspace import send_chat
        target = details.get("space") or details.get("person_name") or details.get("to", "")
        r = _run_async(send_chat(target, details.get("message", details.get("body", ""))))
        if isinstance(r, dict) and r.get("error"):
            return r
        return {"status": "gchat_sent", **(r if isinstance(r, dict) else {"raw": str(r)})}
    except Exception as e:
        return {"error": f"GChat send failed: {e}"}


def _exec_create_branch(details: dict) -> dict:
    from tools.github_tools import create_branch
    return create_branch(owner=details.get("owner", ""), repo=details.get("repo", ""),
                         branch_name=details.get("branch_name", ""), from_ref=details.get("from_ref", "main"))


def _exec_create_pr(details: dict) -> dict:
    """Open a PR. The coding agent is denied `git push`, so the broker (trusted)
    pushes the local branch here first, then opens the PR."""
    import subprocess
    head = details.get("head", "")
    local_dir = details.get("local_dir", "")
    if local_dir and head:
        try:
            push = subprocess.run(
                ["git", "-C", local_dir, "push", "-u", "origin", head],
                capture_output=True, text=True, timeout=120)
            if push.returncode != 0 and "up-to-date" not in (push.stderr + push.stdout).lower():
                logger.warning("branch push non-zero: %s", push.stderr[:200])
        except Exception as e:
            return {"error": f"branch push failed: {e}"}
    from tools.github_tools import create_pr
    return create_pr(owner=details.get("owner", ""), repo=details.get("repo", ""),
                    title=details.get("title", ""), body=details.get("body", ""),
                    head=head, base=details.get("base", "main"))


def _exec_write_agent_file(details: dict) -> dict:
    """Approved create_agent/self_modify file write — ONLY inside the repo's
    .claude/ (agents/skills). Everything else is rejected."""
    import os
    from config.paths import REPO_ROOT
    raw = details.get("path") or details.get("agent_path", "")
    # agent_path is repo-relative (.claude/agents/x.md); path may be absolute
    path = raw if os.path.isabs(raw) else os.path.join(str(REPO_ROOT), raw)
    path = os.path.realpath(path)
    allowed = os.path.realpath(str(REPO_ROOT / ".claude"))
    if not path.startswith(allowed + os.sep):
        return {"error": f"path outside .claude/: {path}"}
    if not path.endswith(".md"):
        return {"error": "only .md agent/skill files may be written"}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        prev = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                prev = f.read()
        with open(path, "w", encoding="utf-8") as f:
            f.write(details["content"])
        return {"status": "written", "path": path, "replaced": prev is not None}
    except OSError as e:
        return {"error": f"write failed: {e}"}


def _exec_add_mcp_server(details: dict) -> dict:
    """Approved add_mcp_server — MERGE one server into the repo's .mcp.json (never
    overwrite the file). Refuses a raw secret in the config: any header value that
    looks like a live key must instead be a ${ENV_VAR} reference (the key lives in the
    keychain/env, exactly like context7). Keeps a .bak. `details` = {name, config}."""
    import os, json as _json, re as _re
    from config.paths import REPO_ROOT
    name = (details.get("name") or "").strip()
    cfg = details.get("config")
    if not name or not isinstance(cfg, dict):
        return {"error": "add_mcp_server needs {name, config:{...}}"}
    # reject a raw secret embedded in the server config — must be a ${VAR} ref
    blob = _json.dumps(cfg)
    for m in _re.findall(r'"[^"]*"', blob):
        v = m.strip('"')
        if _re.search(r"(sk-[a-zA-Z0-9]{16,}|rba_[A-Za-z0-9_]{10,}|ghp_[A-Za-z0-9]{20,}|"
                      r"AIza[A-Za-z0-9_-]{20,})", v) and "${" not in v:
            return {"error": "raw secret in MCP config — reference it as ${ENV_VAR} "
                             "(store the key in the keychain), don't inline it"}
    path = os.path.realpath(str(REPO_ROOT / ".mcp.json"))
    try:
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f) or {}
            # backup before touching a tracked config
            with open(path + ".bak", "w", encoding="utf-8") as b:
                _json.dump(data, b, indent=2)
        servers = data.setdefault("mcpServers", {})
        replaced = name in servers
        servers[name] = cfg
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
        return {"status": "mcp_added", "name": name, "replaced": replaced, "path": path}
    except (OSError, ValueError) as e:
        return {"error": f"mcp merge failed: {e}"}


def _exec_run_command(details: dict) -> dict:
    from tools.system_tools import run_command
    return run_command(command=details.get("command", ""), cwd=details.get("cwd"),
                      timeout=details.get("timeout", 60))


def _exec_acp_spawn(details: dict) -> dict:
    """Spawn a Claude Code ACP session — argv LIST, NO SHELL (injection-proof).

    The argv was built + validated in acp_steer.spawn_proposal; we still defend:
    only allow the exact openclaw-acp-spawn shape and a cwd inside the home tree."""
    import os as _os
    import subprocess
    argv = details.get("argv") or []
    cwd = details.get("cwd", "")
    if (not isinstance(argv, list) or argv[:4] != ["openclaw", "acp", "spawn", "claude"]):
        return {"error": "acp_spawn: unexpected argv shape"}
    home = _os.path.realpath(_os.path.expanduser("~"))
    if not cwd or not _os.path.realpath(cwd).startswith(home) or not _os.path.isdir(cwd):
        return {"error": f"acp_spawn: cwd not inside home / missing: {cwd}"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=45)  # no shell
        return {"status": "acp_spawned" if p.returncode == 0 else "acp_failed",
                "returncode": p.returncode, "out": (p.stdout or p.stderr)[:400]}
    except Exception as e:
        return {"error": f"acp_spawn failed: {e}"}


def _exec_schedule_meeting(details: dict) -> dict:
    try:
        from tools.workspace_tools import create_calendar_event
        result = create_calendar_event(
            summary=details.get("summary", ""), start_time=details.get("start_time", ""),
            end_time=details.get("end_time", ""), attendees=details.get("attendees", []),
            description=details.get("description", ""))
        return {"status": "meeting_scheduled", **result}
    except Exception as e:
        return {"error": f"Meeting scheduling failed: {e}"}


def _exec_edit_file(details: dict) -> dict:
    try:
        from tools.safe_edit import safe_edit
        return safe_edit(file_path=details.get("file_path", ""),
                        new_content=details.get("new_content", ""),
                        description=details.get("description", "agent-edit"))
    except Exception as e:
        return {"error": f"File edit failed: {e}"}
