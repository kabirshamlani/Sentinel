"""Approval Broker — the heart of Sentinel's human-in-the-loop safety.

Implements the owner's exact semantics:
  * Tier R (read/report)  -> execute immediately, no approval.
  * Per-project one-time approval -> once a project + action-type is approved,
    future matching actions auto-execute (report only).
  * Tier X (earned)       -> action-types with a proven success ledger auto-run.
  * Tier A (needs approval):
      - routine   -> deliver to BOTH Telegram + WhatsApp; if no response in
                     30 min, take the best-effort plan, proceed, and report
                     "did X because the approval window expired".
      - critical  -> hold indefinitely; only a human response resolves it.
  * Safety backstop: irreversible + routine is auto-upgraded to critical
    (never do something unrecoverable on a timeout) UNLESS pre-approved.

The broker wraps approval.action_queue (dedup + 7-day rejection + executor) and
adds criticality/timeout/project-scope/dual-channel on top.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from config.paths import PROJECT_SCOPE_PATH
from approval import action_queue as aq

logger = logging.getLogger("broker")

APPROVAL_WINDOW = timedelta(minutes=30)

# action-types that are inherently irreversible / high blast radius -> critical.
# run_command + acp_spawn run host processes, so they hold-for-approval
# structurally (never auto-execute on a routine timeout) regardless of caller hint.
CRITICAL_TYPES = {
    "send_email", "send_gchat", "schedule_meeting", "self_modify", "add_mcp_server",
    "add_skill", "create_agent", "aws_mutation", "create_repo",
    "run_command", "acp_spawn",
}
IRREVERSIBLE_TYPES = CRITICAL_TYPES | {"send_notification"}

# how many clean successes before an action-type earns Tier-X auto-run
TIER_X_THRESHOLD = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- per-project one-time approval ----------

def _load_scope() -> dict:
    if PROJECT_SCOPE_PATH.exists():
        try:
            return json.loads(PROJECT_SCOPE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"projects": {}}


def _save_scope(data: dict) -> None:
    PROJECT_SCOPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_SCOPE_PATH.write_text(json.dumps(data, indent=2))


def is_preapproved(project: str, action_type: str) -> bool:
    if not project:
        return False
    # Defense in depth: self-mod/host-command types are NEVER pre-approved, even
    # if a stale on-disk grant (or a "*") lists them — they always ask.
    if action_type in NEVER_PREAPPROVE:
        return False
    entry = _load_scope()["projects"].get(project)
    if not entry:
        return False
    allowed = entry.get("approved_action_types", [])
    return action_type in allowed or "*" in allowed


# Self-modification / host-command action-types NEVER earn per-project
# auto-approval — a single approval for one of these must not silently green-light
# all future ones (esp. on the `sentinel-self` project). Each stays explicitly gated.
NEVER_PREAPPROVE = {"run_command", "self_modify", "create_agent", "add_mcp_server",
                    "add_skill", "aws_mutation", "acp_spawn", "create_repo"}


def grant_project(project: str, action_type: str, via_action_id: str = "") -> None:
    """After the first approval for a project, pre-approve that action-type (and
    all read/draft actions) for future tasks on that project — EXCEPT
    self-modification/host-command types, which always stay explicitly gated."""
    if not project:
        return
    data = _load_scope()
    entry = data["projects"].setdefault(project, {
        "approved_action_types": [], "granted_at": _now().isoformat(),
        "granted_via": via_action_id,
    })
    for t in (action_type, "draft_email", "draft_gchat", "create_branch", "create_pr"):
        if t and t not in NEVER_PREAPPROVE and t not in entry["approved_action_types"]:
            entry["approved_action_types"].append(t)
    _save_scope(data)
    logger.info("Project pre-approved: %s -> %s", project, entry["approved_action_types"])


def allow_all_for_project(project: str) -> None:
    data = _load_scope()
    data["projects"].setdefault(project, {"approved_action_types": [], "granted_at": _now().isoformat()})
    if "*" not in data["projects"][project]["approved_action_types"]:
        data["projects"][project]["approved_action_types"].append("*")
    _save_scope(data)


# ---------- Tier-X earned autonomy ----------

def _tier_x_earned(action_type: str, project: str = "") -> bool:
    """An action-type earns auto-run once its evidence ledger proves it safe.

    Backed by approval.autonomy (human-approved successes, zero recent failures,
    minimum age; a single rejection/RCA demotes). Never-auto types (sends,
    self-modification, infra mutation) are excluded there."""
    try:
        from approval import autonomy
        return autonomy.is_autonomous(action_type, project)
    except Exception:
        return False


# ---------- criticality classification ----------

def classify_criticality(action_type: str, reversibility: str, criticality_hint: str = "") -> str:
    if criticality_hint in ("routine", "critical"):
        base = criticality_hint
    else:
        base = "critical" if action_type in CRITICAL_TYPES else "routine"
    # safety backstop: irreversible + routine -> critical
    if base == "routine" and (reversibility == "irreversible" or action_type in IRREVERSIBLE_TYPES):
        return "critical"
    return base


# ---------- the broker ----------

class ApprovalBroker:
    """Owns proposal -> delivery -> resolution. Delivery is injected (a channels
    router) so the broker stays testable without live Telegram/WhatsApp."""

    def __init__(self, deliver=None, notify=None):
        # deliver(action) -> dict of {channel: msg_id}; awaited if coroutine
        # notify(text, action) -> post a plain status message; awaited if coroutine
        self._deliver = deliver
        self._notify = notify
        self._waiters: dict[str, asyncio.Event] = {}
        self._resolutions: dict[str, tuple[str, str]] = {}  # action_id -> (decision, channel)

    async def propose(
        self, action_type: str, description: str, details: dict, reasoning: str,
        *, project: str = "", tier: str = "A", reversibility: str = "reversible",
        criticality_hint: str = "", best_effort_plan: str = "", source: str = "conductor",
        blocking: bool = False,
    ) -> dict:
        """Propose an action. blocking=False (default, production): Tier A
        proposals are delivered + persisted with window_expires_at and return
        immediately as 'awaiting_approval' — the long-lived daemon's sweeper and
        resolve() handle timeout/approval so it survives process exits (gap
        P0-1). blocking=True: wait in-process for the resolution (used by tests
        and one-shot CLI kicks that stay alive)."""
        # Tier R: execute now, no approval
        if tier == "R":
            return {"status": "executed", "resolved_by": "tier_r", "tier": "R",
                    "result": aq._execute({"type": action_type, "details": details})}

        # Per-project pre-approval
        if is_preapproved(project, action_type):
            act = aq.propose_action(action_type, description, details, reasoning, source=source,
                                    project=project, tier=tier, reversibility=reversibility)
            if act.get("status") == "skipped":
                return act
            done = aq.approve_action(act["id"], resolved_by="auto_preapproved")
            await self._maybe_notify(f"✅ Auto-approved (project `{project}` pre-approved): {description}", done)
            return done or act

        # Tier X earned autonomy (evidence-driven; never for sends/self-mods)
        if tier == "X" or _tier_x_earned(action_type, project):
            act = aq.propose_action(action_type, description, details, reasoning, source=source,
                                    project=project, tier="X", reversibility=reversibility)
            if act.get("status") == "skipped":
                return act
            done = aq.approve_action(act["id"], resolved_by="tier_x_earned")
            await self._maybe_notify(f"🤖 Auto-ran (earned autonomy): {description}", done)
            return done or act

        # Tier A: needs approval
        criticality = classify_criticality(action_type, reversibility, criticality_hint)
        act = aq.propose_action(action_type, description, details, reasoning, source=source,
                                project=project, criticality=criticality, tier="A",
                                reversibility=reversibility, best_effort_plan=best_effort_plan)
        if act.get("status") == "skipped":
            return act

        # deliver to BOTH channels
        msg_ids = await self._call(self._deliver, act) if self._deliver else {}
        expires = None if criticality == "critical" else (_now() + APPROVAL_WINDOW).isoformat()
        aq.update_action(act["id"], channels_sent=list(msg_ids or {}), delivery_msg_ids=msg_ids or {},
                         window_expires_at=expires)

        if not blocking:
            # Production: persist and return. The daemon sweeper/resolve finishes it.
            return {"status": "awaiting_approval", "id": act["id"],
                    "criticality": criticality, "window_expires_at": expires}

        self._waiters[act["id"]] = asyncio.Event()
        return await self._await_resolution(act["id"], criticality)

    async def _await_resolution(self, action_id: str, criticality: str) -> dict:
        event = self._waiters[action_id]
        timeout = None if criticality == "critical" else APPROVAL_WINDOW.total_seconds()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            decision, channel = self._resolutions.get(action_id, ("approve", "unknown"))
            if decision == "approve":
                done = aq.approve_action(action_id, resolved_by=f"user:{channel}")
                act = aq.get_action(action_id) or {}
                if act.get("project"):
                    grant_project(act["project"], act["type"], action_id)
                return done or {}
            else:
                return aq.reject_action(action_id, reason=f"rejected via {channel}",
                                        resolved_by=f"user:{channel}") or {}
        except asyncio.TimeoutError:
            # routine + no response in 30 min -> best-effort, proceed, report
            act = aq.get_action(action_id) or {}
            aq.update_action(action_id, resolved_by="timeout_best_effort")
            done = aq.approve_action(action_id, resolved_by="timeout_best_effort")
            # silence != standing consent: a timeout executes this one action but
            # does NOT grant_project -- only an explicit user approval earns that.
            plan = act.get("best_effort_plan") or act.get("description", "")
            await self._maybe_notify(
                f"⏱️ Approval window (30 min) expired — I went ahead with the best approach:\n"
                f"• {act.get('description','')}\n• Plan: {plan}\n(Reply to undo if wrong.)", done)
            return done or {}
        finally:
            self._waiters.pop(action_id, None)
            self._resolutions.pop(action_id, None)

    def resolve(self, action_id: str, decision: str, channel: str = "telegram") -> bool:
        """Called by the channel layer when a human taps approve/reject on EITHER
        channel. First response wins."""
        if action_id in self._waiters and not self._waiters[action_id].is_set():
            self._resolutions[action_id] = (decision, channel)
            self._waiters[action_id].set()
            return True
        return False

    async def _call(self, fn, *a):
        if fn is None:
            return None
        r = fn(*a)
        return await r if asyncio.iscoroutine(r) else r

    async def _maybe_notify(self, text: str, action: dict | None):
        if self._notify:
            await self._call(self._notify, text, action)
