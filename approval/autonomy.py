"""Earned autonomy (Tier-X) — action-types graduate to auto-run with EVIDENCE.

The owner chose "earn autonomy per action-type over time." The rule (research doc 11
§f — verifiers are the product, self-report is poison):

  An (action_type, project) pair earns auto-run+notify ONLY when its ledger shows
  >= GRADUATE_THRESHOLD human-approved successes, ZERO rejections/failures in the
  trailing window, and it's past a minimum age. A SINGLE rejection or RCA
  correction DEMOTES it back to approval-gated immediately.

Never graduate: irreversible/external/self-modifying types (send_email, create_pr
to prod, aws_mutation, self_modify, create_agent…). Those stay human-gated
forever — earned autonomy is for repetitive SAFE actions, not the scary ones.

Evidence is the completion/rejection logs the broker already writes, plus this
module's own grant ledger. Deterministic Python decides; no LLM in the loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from config.paths import SENTINEL_HOME

LEDGER_PATH = SENTINEL_HOME / "autonomy_ledger.json"
GRADUATE_THRESHOLD = 8         # clean human-approved successes to earn autonomy
WINDOW_DAYS = 30               # evidence window
MIN_AGE_DAYS = 3               # can't graduate on day one even if lucky

# Types that can NEVER auto-run, no matter how clean the record.
NEVER_AUTO = {
    "send_email", "send_gchat", "self_modify", "create_agent", "add_mcp_server",
    "add_skill", "aws_mutation", "create_repo", "schedule_meeting",
    "acp_spawn", "run_command",   # spawning host processes never auto-runs
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(action_type: str, project: str) -> str:
    return f"{action_type}::{project or '_'}"


def _load() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"records": {}}


def _save(data: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _rec(data: dict, key: str) -> dict:
    return data["records"].setdefault(key, {
        "successes": [], "failures": [], "first_seen": _now().isoformat(),
        "graduated": False, "graduated_at": None, "demoted_at": None,
    })


def record_success(action_type: str, project: str = "", resolved_by: str = "") -> None:
    """A human-approved (not timeout) action executed cleanly."""
    if resolved_by.startswith("timeout"):
        return                       # silence != evidence of correctness
    data = _load()
    r = _rec(data, _key(action_type, project))
    r["successes"].append(_now().isoformat())
    r["successes"] = r["successes"][-50:]
    _save(data)
    _maybe_graduate(action_type, project)


def record_failure(action_type: str, project: str = "", reason: str = "") -> None:
    """A rejection, RCA correction, or execution failure — DEMOTES immediately."""
    data = _load()
    r = _rec(data, _key(action_type, project))
    r["failures"].append({"at": _now().isoformat(), "reason": reason[:120]})
    r["failures"] = r["failures"][-50:]
    if r.get("graduated"):
        r["graduated"] = False
        r["demoted_at"] = _now().isoformat()
    _save(data)


def _in_window(iso_list) -> list:
    cutoff = (_now() - timedelta(days=WINDOW_DAYS)).isoformat()
    out = []
    for x in iso_list:
        ts = x if isinstance(x, str) else x.get("at", "")
        if ts >= cutoff:
            out.append(x)
    return out


def _maybe_graduate(action_type: str, project: str) -> bool:
    if action_type in NEVER_AUTO:
        return False
    data = _load()
    r = _rec(data, _key(action_type, project))
    if r.get("graduated"):
        return True
    succ = _in_window(r["successes"])
    fails = _in_window(r["failures"])
    try:
        age_days = (_now() - datetime.fromisoformat(r["first_seen"])).days
    except ValueError:
        age_days = 0
    if len(succ) >= GRADUATE_THRESHOLD and len(fails) == 0 and age_days >= MIN_AGE_DAYS:
        r["graduated"] = True
        r["graduated_at"] = _now().isoformat()
        _save(data)
        return True
    return False


def is_autonomous(action_type: str, project: str = "") -> bool:
    """Broker asks this: may this (type, project) auto-run without approval?"""
    if action_type in NEVER_AUTO:
        return False
    data = _load()
    r = data["records"].get(_key(action_type, project))
    if not r or not r.get("graduated"):
        return False
    # defensive re-check: any failure in-window revokes even a graduated grant
    return len(_in_window(r.get("failures", []))) == 0


def status_text() -> str:
    """For a chat/report view of what's earned autonomy."""
    data = _load()
    grad, learning = [], []
    for key, r in data["records"].items():
        at, proj = key.split("::")
        succ = len(_in_window(r["successes"]))
        if r.get("graduated"):
            grad.append(f"• {at} ({proj}) — auto-runs, {succ} clean approvals")
        elif at not in NEVER_AUTO and succ > 0:
            learning.append(f"• {at} ({proj}) — {succ}/{GRADUATE_THRESHOLD} toward autonomy")
    lines = ["🎓 **Earned autonomy**"]
    lines += grad or ["(nothing has earned auto-run yet)"]
    if learning:
        lines.append("\nLearning trust:")
        lines += learning
    lines.append(f"\n(A single rejection or correction demotes an action back to "
                 f"asking you. Sends/self-changes never auto-run.)")
    return "\n".join(lines)
