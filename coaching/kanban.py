"""Kanban — the durable issue board the fix heartbeat picks from.

Backend: **beads** (`bd`, github.com/gastownhall/beads) in a DEDICATED database
at ~/.sentinel/coaching/beads/ — deliberately outside the sentinel code repo so
board writes never dirty fix-branch diffs. Beads gives what the flat JSON store
was slowly reinventing: typed dependency links (regressions point at their
original), blocker-aware ready-work detection, atomic claims, and Dolt-backed
history. Adopted 2026-07-29 on the owner's call — the workload had grown
graph-shaped (regression_of links, shared root causes, multi-step plans),
which is exactly the evidence bar docs 16/17 set for this migration.

Design invariants (unchanged from v1):
  * The coach's row (rca_class, evidence, plan, fix, github, attempts…) is the
    source of truth — stored WHOLE in the bead's metadata; bd status/labels
    are kept in sync for graph semantics and human `bd` CLI browsing.
  * Same ids everywhere: our ci-<hash> ids ARE the bead ids (bd --id).
  * Statuses: backlog -> open · in_progress -> in_progress · in_review ->
    blocked (never re-picked while a human/PM holds it) · done|rejected ->
    closed.
  * Dedup by fingerprint; a rejection stays quiet REJECT_QUIET_DAYS; a
    regression refile gets a unique id + a discovered-from link to (and GitHub
    reopen of) the original.
  * If `bd` is unavailable, the legacy JSON store keeps the loop alive
    (graceful degradation — a missing binary must never stop coaching).

Columns: backlog -> in_progress -> in_review (fix branch + broker proposal
raised, awaiting approval) -> done (merge verified with evidence) | rejected.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coaching.episodes import COACH_DIR

logger = logging.getLogger("coach.kanban")

KANBAN_PATH = COACH_DIR / "kanban.json"          # legacy/fallback store
BEADS_DIR = COACH_DIR / "beads"
STATUSES = ("backlog", "in_progress", "in_review", "done", "rejected")
REJECT_QUIET_DAYS = 14
OPEN_STATUSES = ("backlog", "in_progress", "in_review")
MAX_FIX_ATTEMPTS = 3

# local status <-> bd status (blocked = held for human/PM, never in bd ready)
_TO_BD = {"backlog": "open", "in_progress": "in_progress",
          "in_review": "blocked", "done": "closed", "rejected": "closed"}
_SEV_PRIO = {"high": "1", "medium": "2", "low": "3"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────── backend plumbing ───────────────────────────

def backend() -> str:
    if (BEADS_DIR / ".beads").exists() and shutil.which("bd"):
        return "beads"
    return "json"


def _bd(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        res = subprocess.run(["bd", *args], capture_output=True, text=True,
                             cwd=str(BEADS_DIR), timeout=timeout)
        return res.returncode, (res.stdout or res.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _bd_json(*args: str) -> list | dict | None:
    rc, out = _bd(*args, "--json")
    if rc != 0:
        logger.warning("bd %s failed: %s", " ".join(args[:2]), out[:150])
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _row_from_bead(bead: dict) -> dict | None:
    """Our row = the bead's metadata (source of truth), id from the bead."""
    meta = bead.get("metadata") or {}
    if not isinstance(meta, dict) or "fingerprint" not in meta:
        return None                     # not a coaching bead
    row = dict(meta)
    row["id"] = bead.get("id", row.get("id", ""))
    return row


def _write_bead_state(row: dict) -> bool:
    """Push the row's metadata + mapped status onto its bead."""
    bd_status = _TO_BD.get(row.get("status", "backlog"), "open")
    rc, out = _bd("update", row["id"], "-s", bd_status,
                  "--metadata", json.dumps(row, ensure_ascii=False))
    if rc != 0:
        logger.warning("bd update %s failed: %s", row["id"], out[:150])
    return rc == 0


# ─────────────────────────── legacy JSON store ───────────────────────────

def _jload() -> dict:
    if KANBAN_PATH.exists():
        try:
            return json.loads(KANBAN_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("kanban unreadable — starting fresh (old file kept as .bak)")
            try:
                KANBAN_PATH.rename(KANBAN_PATH.with_suffix(".json.bak"))
            except OSError:
                pass
    return {"issues": []}


def _jsave(data: dict) -> None:
    import os
    COACH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = KANBAN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, KANBAN_PATH)


# ─────────────────────────── public API ───────────────────────────

def get(issue_id: str) -> dict | None:
    if backend() == "beads":
        bead = _bd_json("show", issue_id)
        if isinstance(bead, list):
            bead = bead[0] if bead else None
        return _row_from_bead(bead) if bead else None
    for i in _jload()["issues"]:
        if i.get("id") == issue_id:
            return i
    return None


def issues(status: str | None = None) -> list[dict]:
    if backend() == "beads":
        beads = _bd_json("list", "--all") or []
        rows = [r for r in (_row_from_bead(b) for b in beads) if r]
        rows.sort(key=lambda r: r.get("created", ""))
    else:
        rows = _jload()["issues"]
    if status:
        rows = [i for i in rows if i.get("status") == status]
    return rows


def _blocked_by_dedup(existing: dict) -> bool:
    st = existing.get("status", "")
    if st in OPEN_STATUSES:
        return True
    if st == "rejected":
        try:
            resolved = datetime.fromisoformat(existing.get("updated", existing.get("created", "")))
            return datetime.now(timezone.utc) - resolved < timedelta(days=REJECT_QUIET_DAYS)
        except ValueError:
            return True
    return False   # done issues may recur — a regression re-files


def _already_fixed(issue: dict, resolved_at: str) -> bool:
    """True if a 'new' occurrence is really a straggler of the already-fixed
    bug, not a genuine regression — so it must dedup, never re-file as -rN and
    waste fix cycles on a defect that no longer reproduces.

    Two independent signals (either is sufficient):
      A. WIRING issues (static repo checks — journal_field_mismatch,
         has_call_site, slug_map_covers_agent, agent_md_exists, artifact_clean,
         unobservable): re-run the check against CURRENT code; if it no longer
         fires, the defect is gone. These have no episode_ts, which is exactly
         why the old timestamp-only guard let them slip through (the root cause
         of the ci-03096f42-r1 loop).
      B. EPISODE issues: all evidence timestamps strictly precede resolved_at.
    """
    check = issue.get("check", "")
    # A) live re-verification of wiring checks
    try:
        from coaching.checks import WIRING_CHECKS
        if check in WIRING_CHECKS:
            # reconstruct params from the rubric that owns this check
            from coaching.rubrics import load_rubrics
            params = {}
            for r in load_rubrics().values():
                for wc in (r.get("wiring_checks") or []):
                    if wc.get("check") == check:
                        params = wc.get("params") or {}
            still_fires = bool(WIRING_CHECKS[check](params))
            return not still_fires
    except Exception:
        pass  # fall through to the timestamp signal
    # B) timestamp signal for episode-based issues
    evidence = issue.get("evidence") or []
    if not evidence:
        return False
    try:
        resolved = datetime.fromisoformat(resolved_at)
    except (TypeError, ValueError):
        return False
    for entry in evidence:
        ts = entry.get("episode_ts") if isinstance(entry, dict) else None
        if not ts:
            return False
        try:
            if datetime.fromisoformat(ts) >= resolved:
                return False
        except ValueError:
            return False
    return True


def file_issue(issue: dict) -> dict:
    """Add an issue to the backlog (dedup by fingerprint). Returns
    {status: filed|duplicate, id}."""
    rows = issues()
    regression_of = None
    for existing in rows:
        if existing.get("fingerprint") != issue.get("fingerprint"):
            continue
        if _blocked_by_dedup(existing):
            return {"status": "duplicate", "id": existing["id"],
                    "existing_status": existing.get("status")}
        if existing.get("status") == "done":
            resolved_at = existing.get("updated", existing.get("created", ""))
            if _already_fixed(issue, resolved_at):
                # stragglers of the already-fixed bug are not regressions
                return {"status": "duplicate", "id": existing["id"],
                        "existing_status": existing.get("status")}
            regression_of = existing["id"]

    row = dict(issue)
    if regression_of:
        row["regression_of"] = regression_of
        gen = sum(1 for i in rows if i.get("fingerprint") == issue.get("fingerprint"))
        row["id"] = f"{row['id']}-r{gen}"
    row.setdefault("status", "backlog")
    row.setdefault("created", _now())
    row["updated"] = row["created"]
    row.setdefault("fix", {})
    row.setdefault("github", {})
    row.setdefault("attempts", 0)

    if backend() == "beads":
        labels = ",".join(filter(None, (
            "coaching", f"agent:{row.get('agent','')}", f"severity:{row.get('severity','')}",
            f"rca:{row.get('rca_class','')}", "regression" if regression_of else "")))
        rc, out = _bd("create", row.get("title", row["id"])[:200],
                      "--id", row["id"], "--force",
                      "-d", (row.get("rca_why", "") or "")[:800],
                      "-l", labels,
                      "-p", _SEV_PRIO.get(row.get("severity", "medium"), "2"),
                      "--metadata", json.dumps(row, ensure_ascii=False))
        if rc != 0:
            logger.warning("bd create %s failed (%s) — json fallback", row["id"], out[:150])
            data = _jload(); data["issues"].append(row); _jsave(data)
        elif regression_of:
            _bd("link", row["id"], regression_of, "--type", "discovered-from")
    else:
        data = _jload()
        data["issues"].append(row)
        _jsave(data)

    logger.info("kanban[%s]: filed %s (%s, %s)%s", backend(), row["id"],
                row.get("agent"), row.get("severity"),
                f" regression of {regression_of}" if regression_of else "")
    return {"status": "filed", "id": row["id"]}


def move(issue_id: str, status: str, **fields) -> bool:
    if status not in STATUSES:
        return False
    if backend() == "beads":
        row = get(issue_id)
        if not row:
            return False
        row["status"] = status
        row["updated"] = _now()
        for k, v in fields.items():
            if k == "fix" and isinstance(v, dict):
                row.setdefault("fix", {}).update(v)
            else:
                row[k] = v
        return _write_bead_state(row)
    data = _jload()
    for i in data["issues"]:
        if i.get("id") == issue_id:
            i["status"] = status
            i["updated"] = _now()
            for k, v in fields.items():
                if k == "fix" and isinstance(v, dict):
                    i.setdefault("fix", {}).update(v)
                else:
                    i[k] = v
            _jsave(data)
            return True
    return False


def pick_next() -> dict | None:
    """Highest-severity, most-recurrent READY issue that hasn't exhausted its
    fix attempts. On beads, 'ready' is blocker-aware: an issue whose
    dependency is still open is never picked before its blocker."""
    sev = {"high": 0, "medium": 1, "low": 2}
    if backend() == "beads":
        ready = _bd_json("ready") or []
        rows = [r for r in (_row_from_bead(b) for b in ready) if r]
        rows = [r for r in rows if r.get("status") == "backlog"
                and int(r.get("attempts", 0)) < MAX_FIX_ATTEMPTS]
    else:
        rows = [i for i in issues("backlog")
                if int(i.get("attempts", 0)) < MAX_FIX_ATTEMPTS]
    if not rows:
        return None
    rows.sort(key=lambda i: (sev.get(i.get("severity"), 1), -int(i.get("count", 1)),
                             i.get("created", "")))
    return rows[0]


def board_summary() -> dict:
    counts: dict[str, int] = {}
    for i in issues():
        counts[i.get("status", "?")] = counts.get(i.get("status", "?"), 0) + 1
    return counts


def reconcile() -> dict:
    """Close the loop broker→board: for every in_review issue, read the REAL
    resolution (action_queue status + git merge state) and move the row.
    `done` requires evidence — the fix commit must actually be an ancestor of
    the repo HEAD (the approval executor ran the merge), never just an
    'approved' flag. Rejected proposals move to rejected (14-day quiet)."""
    from config.paths import REPO_ROOT
    moved = {"done": [], "rejected": [], "pending": []}
    for i in issues("in_review"):
        fx = i.get("fix") or {}
        action_id, commit = fx.get("action_id", ""), fx.get("commit", "")
        status = ""
        if action_id:
            try:
                from approval import action_queue as aq
                act = aq.get_action(action_id) or {}
                status = act.get("status", "")
            except Exception:
                status = ""
        merged = False
        if commit:
            try:
                merged = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                    cwd=str(REPO_ROOT), capture_output=True, timeout=15
                ).returncode == 0
            except Exception:
                merged = False
        if merged:
            move(i["id"], "done", fix={"merged_evidence": f"commit {commit} is ancestor of HEAD"})
            moved["done"].append(i["id"])
            if fx.get("worktree"):
                subprocess.run(["git", "worktree", "remove", "--force", fx["worktree"]],
                               cwd=str(REPO_ROOT), capture_output=True, timeout=30)
        elif status == "rejected":
            move(i["id"], "rejected", fix={"note": "proposal rejected by human"})
            moved["rejected"].append(i["id"])
            if fx.get("worktree"):
                subprocess.run(["git", "worktree", "remove", "--force", fx["worktree"]],
                               cwd=str(REPO_ROOT), capture_output=True, timeout=30)
        else:
            moved["pending"].append(i["id"])
    return moved


async def mirror_to_github(issue: dict, broker) -> dict:
    """Propose the GitHub mirror through the broker (routine tier — creating
    an issue is reversible and low-blast-radius; the PR stays critical).
    Best-effort: no token / no network just leaves the local row authoritative.
    On execution, the created issue's number/url are written back to the row.

    A REGRESSION refile never creates a new GitHub issue (the broker's dedup
    would rightly block the near-identical title anyway) — it inherits the
    original row's issue and REOPENS it, so the history stays on one ticket."""
    try:
        import os
        from tools.github_tools import _keychain_token
        if not (os.getenv("GITHUB_TOKEN") or _keychain_token()):
            return {"status": "skipped", "reason": "no github token"}

        if issue.get("regression_of"):
            original = get(issue["regression_of"]) or {}
            ogh = original.get("github") or {}
            if ogh.get("number"):
                gh = dict(issue.get("github") or {})
                gh.update({"number": ogh["number"], "url": ogh.get("url", "")})
                move(issue["id"], issue.get("status", "backlog"), github=gh)
                return await broker.propose(
                    action_type="update_issue",
                    description=f"Reopen coaching issue #{ogh['number']}: regression "
                                f"{issue['id']} of {issue['regression_of']}",
                    details={"owner": "your-org", "repo": "sentinel",
                             "number": ogh["number"], "state": "open",
                             "labels": ["coaching", "regression",
                                        f"status:{issue.get('status','backlog')}"]},
                    reasoning="Regression of a fixed issue — history stays on the original ticket.",
                    project="sentinel-self")

        body = (
            f"**Coach-filed issue** `{issue['id']}` — RCA class **{issue['rca_class']}**\n\n"
            f"Agent: `{issue['agent']}` · check: `{issue['check']}` · "
            f"occurrences: {issue.get('count', 1)}\n\n"
            f"Why this class: {issue.get('rca_why','')}\n\n"
            f"Fix channel: {issue.get('fix_channel','')} → `{issue.get('fix_target','')}`\n\n"
            "Evidence:\n" +
            "\n".join(f"- `{e.get('stream','')}` {e.get('evidence','')[:160]}"
                      for e in issue.get("evidence", [])[:5])
        )
        res = await broker.propose(
            action_type="create_issue",
            description=f"File coaching issue on GitHub: {issue['title'][:80]}",
            details={"owner": "your-org", "repo": "sentinel",
                     "title": f"[coach] {issue['title'][:100]}",
                     "body": body, "labels": ["coaching", "bug"]},
            reasoning="Mirror of a locally-filed coaching issue so it is visible on the board.",
            project="sentinel-self",
        )
        exec_result = (res or {}).get("execution_result") or (res or {}).get("result") or {}
        if isinstance(exec_result, dict) and exec_result.get("url"):
            move(issue["id"], issue.get("status", "backlog"),
                 github={"number": exec_result.get("number"), "url": exec_result["url"]})
            if backend() == "beads":
                _bd("update", issue["id"], "--external-ref", f"gh-{exec_result.get('number')}")
        return res
    except Exception as e:
        logger.debug("github mirror failed: %s", e)
        return {"status": "error", "reason": str(e)[:200]}
