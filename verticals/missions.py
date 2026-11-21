"""Missions — perceive an ask, EXECUTE it now, deliver finished work.

THE GAP THIS CLOSES (the owner, 2026-08-14): "Alex mailed us to have a common
registry endpoint for knowledge base … it should have understood where the
knowledge bases are, done the task, and reported back: 'this task has been done and
here is the draft to be sent to Alex.' I did that task manually."

Until now the ONLY thing Sentinel could do with an inbound ask was draft a
reply (inbox_triage, capped by design); the only autonomous execution path
watched GitHub issues on a test repo. This vertical is the missing bridge:

    timeline.hot_threads()  →  LLM detect (is this a task? how urgent?)
    →  EXECUTE IMMEDIATELY  (act-tier Claude Code in an isolated worktree /
                             scratch dir — never the owner's live checkout,
                             never idle-gated: work starts when the ask lands)
    →  DELIVER              (✅ done-report to the owner + the draft reply as ONE
                             approval tap; for code, a PR proposal)

Safety model (unchanged walls, same as github_autopilot which also runs
attacker-influenced content through an act-tier agent):
  * deny-guard travels via --settings on every tier → the mission agent
    physically cannot `gog … send`, `git push`, `rm -rf`, or egress secrets;
  * every outbound send/PR goes through the broker → the owner's tap;
  * mission workspaces live under ~/.sentinel/missions/<id>/ or a repo
    worktree — parallel to the owner's own work, not on top of it;
  * detect + execute prompts carry the trusted/untrusted fence (message
    content is DATA, not instructions).

Honesty model: a mission closes `done` only with Python-captured evidence
(the broker action id of the delivered draft / commit SHA), recorded to the
evidence ledger — never on model say-so (knowledge base DOK-3 #6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from conductor import kv
from memory import timeline

logger = logging.getLogger("missions")

DETECT_THRESHOLD = 6        # urgency >= this → execute now
MAX_CONCURRENT = 2
DAILY_MISSION_CAP = 5
MISSIONS_DIR = Path.home() / ".sentinel" / "missions"

_BOT_RE = re.compile(
    r"(?:no-?reply|notifications?@|mailer-daemon|calendar-notification"
    r"|gemini-notes@|@mail\.|@e\.|newsletter|@github\.com|@google\.com"
    r"|unsubscribe|read\.ai|otter\.ai)", re.I)

_running: set[str] = set()


# ─── perception: pull fresh signals into the timeline ────────────────────

def _sender_parts(raw_from: str) -> tuple[str, str]:
    """'Alex S <alex@x.com>' -> ('Alex S', 'alex@x.com')."""
    m = re.match(r"\s*\"?([^\"<]*)\"?\s*<([^>]+)>", raw_from or "")
    if m:
        return m.group(1).strip() or m.group(2).strip(), m.group(2).strip().lower()
    return (raw_from or "").strip(), (raw_from or "").strip().lower()


def _my_addresses() -> set[str]:
    addrs = {a.strip().lower() for a in
             (kv.get("my_addresses", []) or []) if isinstance(a, str)}
    addrs.add("owner@example.com")
    return addrs


async def perceive_email(hours: int = 48, max_items: int = 25) -> int:
    """Recent inbox mail → timeline. Deliberately NOT is:unread — the owner reading
    an email on his phone doesn't mean he handled it; the ask must stay visible
    until acted on. Idempotent ingest makes the re-scan free."""
    try:
        from tools import gog_workspace as gw
        if not gw.available():
            return 0
        days = max(1, round(hours / 24))
        emails = await gw.search_emails(f"in:inbox newer_than:{days}d", max_items) or []
    except Exception as e:
        logger.warning("mission perceive_email failed: %s", e)
        return 0

    mine = _my_addresses()
    events = []
    for em in emails:
        if not isinstance(em, dict):
            continue
        raw_from = em.get("from", "")
        sender, key = _sender_parts(raw_from)
        kind = "message"
        if key in mine:
            kind = "self"
        elif _BOT_RE.search(raw_from) or _BOT_RE.search(em.get("subject", "")):
            kind = "bot"
        events.append({
            "id": em.get("id", ""), "source": "gmail",
            "ts": _to_iso(em.get("date", "")),
            "sender": sender, "sender_key": key, "channel": "inbox",
            "kind": kind,
            "text": f"{em.get('subject', '')} — {em.get('snippet', '')}".strip(" —"),
            "meta": {"subject": em.get("subject", ""), "thread_id": em.get("thread_id", "")},
        })
    return timeline.ingest(events)


async def perceive_cursor() -> int:
    """Recent Cursor chat sessions → timeline (kind='self': The owner's own work
    context — excluded from ask-clusters, but it builds the rolling 'what has
    the owner been doing' history the mission briefs and recall draw on).

    THROTTLED to every 6h and run in a THREAD: the Cursor state.vscdb is ~79GB
    and the bubbleId queries can take minutes — run synchronously they froze
    the daemon's whole event loop (found live, first scan after v0.17)."""
    stamp = kv.get("cursor_perceive_at", "")
    if stamp:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(stamp)).total_seconds()
            if age < 6 * 3600:
                return 0
        except ValueError:
            pass
    try:
        from tools import observer
        rows = await asyncio.to_thread(observer.cursor_sessions, 26, True)
    except Exception as e:
        logger.debug("cursor perceive failed: %s", e)
        return 0
    kv.put("cursor_perceive_at", kv.now_iso())
    events = []
    for r in rows:
        asks = "; ".join(r.get("recent_asks", []) or [])
        events.append({
            # daily granularity: one row per session per day, not per poll
            "id": f"{r.get('composer_id','')}:{str(r.get('last_active',''))[:10]}",
            "source": "cursor", "ts": r.get("last_active", ""),
            "sender": "the owner (Cursor)", "sender_key": "owner:cursor",
            "channel": r.get("folder", ""), "kind": "self",
            "text": f"{r.get('name','')} — {asks}".strip(" —")[:800],
            "meta": {"files_changed": r.get("files_changed", 0)},
        })
    return timeline.ingest(events)


def _to_iso(date_str: str) -> str:
    """Best-effort RFC2822/loose date -> ISO UTC; fall back to now."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        try:
            dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()


# ─── detection: is there a task in this cluster? ─────────────────────────

def _day_context() -> str:
    parts = []
    try:
        from verticals.day import context_block
        parts.append(context_block() or "")
    except Exception:
        pass
    try:
        from tools import observer
        parts.append(observer.summary_text(max_chars=800) or "")
    except Exception:
        pass
    out = "\n\n".join(p for p in parts if p).strip()
    return out or "(no day context)"


async def _enrich_cluster(cluster: dict) -> None:
    """Thin gmail events (subject-only — empty snippet) get the real body read
    in before judgment; a detector that can't see the ask can't judge it.
    Persisted back to the timeline so the read happens once."""
    try:
        from tools import gog_workspace as gw
    except Exception:
        return
    for e in cluster.get("events", []):
        if e.get("source") != "gmail" or len(e.get("text", "")) >= 150:
            continue
        msg_id = e["id"].split(":", 1)[-1]
        try:
            full = await gw.read_email(msg_id) or {}
            body = (full.get("body") or full.get("snippet") or "").strip()
        except Exception:
            body = ""
        if body:
            e["text"] = (e.get("text", "") + "\n" + body)[:1800]
            timeline.update_text(e["id"], e["text"])


async def _detect(cluster: dict) -> dict | None:
    """LLM judgment over one hot cluster. Returns the parsed verdict or None."""
    from brain import providers
    from prompts import load
    await _enrich_cluster(cluster)
    prompt = load("mission.detect",
                  day_context=_day_context(),
                  cluster=timeline.render_cluster(cluster))
    r = await providers.call_model("claude-sonnet-5", prompt, max_tokens=500, timeout=90)
    if r.get("error"):
        logger.warning("mission detect LLM failed: %s", r["error"][:150])
        return None
    text = (r.get("text") or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        verdict = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(verdict, dict):
        return None
    return verdict


# ─── the scan entrypoint (daemon, every ~10 min, NEVER idle-gated) ────────

async def perceive() -> int:
    """Pull fresh signals into the timeline. Runs regardless of which brain
    (Hermes harness or the internal fallback) does the judging."""
    n = await perceive_email()
    n += await perceive_cursor()
    return n


def queue_from_cluster(cluster: dict, verdict: dict) -> tuple[dict | None, str]:
    """Open a mission (status 'queued') from a hot cluster + a judgment.
    Shared by the internal scan AND the Hermes bridge — the daily cap and the
    timeline marking live here so no judge can bypass them. The daemon's
    executor picks queued missions up within a minute."""
    today = datetime.now().date().isoformat()
    ledger = kv.get("mission_ledger", {}) or {}
    if ledger.get("date") != today:
        ledger = {"date": today, "opened": 0}
    if ledger["opened"] >= DAILY_MISSION_CAP:
        return None, f"daily mission cap ({DAILY_MISSION_CAP}) reached"
    mission = _open_mission(cluster, verdict)
    ledger["opened"] += 1
    kv.put("mission_ledger", ledger)
    return mission, "queued"


async def run_queued(broker, router) -> int:
    """Start executing queued missions (bounded by MAX_CONCURRENT). Called by
    the daemon every minute — the daemon is the ONLY executor host, so a
    mission survives whichever brain queued it exiting."""
    missions_kv = kv.get("missions", {}) or {}
    queued = sorted((m for m in missions_kv.values() if m.get("status") == "queued"),
                    key=lambda m: m.get("created", ""))
    started = 0
    for m in queued:
        if len(_running) >= MAX_CONCURRENT:
            break
        if m["id"] in _running:
            continue
        m["status"] = "executing"
        _save(m)
        _running.add(m["id"])
        asyncio.create_task(_run_mission(m, broker, router))
        started += 1
    return started


async def scan(broker, router) -> dict:
    """INTERNAL judge: correlate → detect → queue. The primary judge is the
    Hermes harness (brain/hermes_harness.py); this is the same doctrine run by
    the in-process LLM, used as the fallback when Hermes is unavailable."""
    today = datetime.now().date().isoformat()
    seen = kv.get("mission_seen", {}) or {}
    started = []

    for cluster in timeline.hot_threads():
        if len(_running) >= MAX_CONCURRENT:
            break
        newest_id = cluster["events"][-1]["id"]
        if newest_id in seen:
            continue

        verdict = await _detect(cluster)
        if verdict is None:
            # LLM failure (outage, unparseable) — do NOT mark seen; the cluster
            # must be re-judged next scan once the brain is back. The canary
            # owns alerting on systemic failure.
            continue
        # A real verdict — including not-actionable — marks the newest event
        # seen so we don't re-judge the identical cluster every 10 minutes.
        # A new message from the same person = new newest_id = fresh look.
        seen[newest_id] = {"at": kv.now_iso(),
                           "actionable": bool(verdict.get("actionable"))}
        if not verdict.get("actionable"):
            continue

        urgency = int(verdict.get("urgency", 0) or 0)
        if urgency < DETECT_THRESHOLD:
            backlog = kv.get("mission_backlog", []) or []
            backlog.append({"at": kv.now_iso(), "task": verdict.get("task", ""),
                            "sender": cluster.get("sender", ""), "urgency": urgency})
            kv.put("mission_backlog", backlog[-20:])
            continue

        mission, reason = queue_from_cluster(cluster, verdict)
        if mission is None:
            logger.info("mission not opened: %s", reason)
            break
        started.append(mission["id"])
        await router.broadcast(
            f"🎯 Picked up: {verdict.get('task','(task)')}\n"
            f"From: {cluster.get('sender','?')} (urgency {urgency}/10 — "
            f"{verdict.get('why','')})\nWorking on it now; you'll get the result "
            f"+ a ready draft, not questions.")

    kv.put("mission_seen", _prune_seen(seen))
    return {"started": started}


def _prune_seen(seen: dict, keep_days: int = 14) -> dict:
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    out = {}
    for k, v in seen.items():
        try:
            at = datetime.fromisoformat(v.get("at", "")).timestamp()
            if at >= cutoff:
                out[k] = v
        except Exception:
            continue
    return out


def _open_mission(cluster: dict, verdict: dict) -> dict:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    mission = {
        "id": mid,
        "created": kv.now_iso(),
        "status": "queued",
        "task": str(verdict.get("task", ""))[:400],
        "deliverable": str(verdict.get("deliverable", ""))[:400],
        "kind": str(verdict.get("kind", "other")),
        "repo": str(verdict.get("repo", "") or ""),
        "urgency": int(verdict.get("urgency", 0) or 0),
        "sender": cluster.get("sender", ""),
        "sender_key": cluster.get("sender_key", ""),
        "sources": cluster.get("sources", []),
        "event_ids": [e["id"] for e in cluster.get("events", [])],
        "cluster_text": timeline.render_cluster(cluster),
    }
    missions = kv.get("missions", {}) or {}
    missions[mid] = mission
    kv.put("missions", missions)
    timeline.mark_mission(mission["event_ids"], mid)
    return mission


# ─── execution ────────────────────────────────────────────────────────────

async def _workspace_for(mission: dict) -> str:
    """Isolated place to work: a repo worktree when the ask names a repo we
    can reach, else a scratch dir. NEVER the owner's live checkout — he keeps
    working in parallel."""
    repo = mission.get("repo", "")
    if repo and "/" in repo:
        try:
            from tools import repo_manager
            owner, name = repo.split("/", 1)
            branch = f"sentinel/mission-{mission['id']}"
            wt = await repo_manager.worktree(owner, name, branch)
            mission["branch"] = branch
            return wt
        except Exception as e:
            logger.warning("mission %s: worktree for %s failed (%s) — scratch dir",
                           mission["id"], repo, e)
    scratch = MISSIONS_DIR / mission["id"]
    scratch.mkdir(parents=True, exist_ok=True)
    return str(scratch)


def _split_sections(text: str) -> dict:
    """Parse the '## RESULT / ## EVIDENCE / ## DRAFT_REPLY / ## FILES' contract."""
    out = {"RESULT": "", "EVIDENCE": "", "DRAFT_REPLY": "", "FILES": ""}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"##\s+(RESULT|EVIDENCE|DRAFT_REPLY|FILES)\s*$", line.strip())
        if m:
            cur = m.group(1)
            continue
        if cur:
            out[cur] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


async def _run_mission(mission: dict, broker, router) -> None:
    mid = mission["id"]
    _running.add(mid)
    try:
        from brain.claude_runner import run_claude
        from prompts import load

        workspace = await _workspace_for(mission)
        mission["workspace"] = workspace
        _save(mission)

        prompt = load("mission.execute",
                      task=mission["task"],
                      deliverable=mission["deliverable"],
                      cluster=mission["cluster_text"],
                      day_context=_day_context(),
                      workspace=workspace)

        task_type = "code_fix" if mission.get("kind") == "code" else "research"
        result_text, is_err = "", False
        async for ev in run_claude(prompt, agent="conductor", tier="act",
                                   working_dir=workspace, max_turns=50,
                                   task_type=task_type, episode_source="mission"):
            if ev.type == "result":
                result_text = ev.content or ""
                is_err = bool(getattr(ev, "is_error", False))
            elif ev.type == "error":
                result_text, is_err = ev.content or "runner error", True

        if is_err or not result_text.strip():
            mission["status"] = "failed"
            mission["error"] = (result_text or "empty result")[:300]
            _save(mission)
            await router.broadcast(
                f"⚠️ Mission failed: {mission['task'][:120]}\n"
                f"{mission['error'][:200]}\nI'll leave it on your plate rather than fake it.")
            _record_ledger(mission, status="failed")
            try:
                from coaching.escalate import live_failure
                live_failure("missions", mission["task"], mission["error"])
            except Exception:
                pass
            return

        await _deliver(mission, _split_sections(result_text), broker, router)

    except Exception as e:
        logger.warning("mission %s crashed: %s", mid, e)
        mission["status"] = "failed"
        mission["error"] = str(e)[:300]
        _save(mission)
    finally:
        _running.discard(mid)


def _save(mission: dict) -> None:
    missions = kv.get("missions", {}) or {}
    missions[mission["id"]] = mission
    kv.put("missions", missions)


def _record_ledger(mission: dict, status: str, evidence: str = "") -> None:
    try:
        from memory import ledger
        ledger.record(mission["task"][:200], "mission",
                      target=mission.get("sender", ""),
                      evidence=evidence, status=status)
    except Exception:
        pass


# ─── delivery: done-report + one-tap draft ────────────────────────────────

def _reply_details(mission: dict, draft: str) -> tuple[str, dict] | None:
    """(action_type, details) for the draft reply, addressed from the
    ORIGINAL events (Python-held metadata) — never from model output.
    Prefers replying on the channel the ask ARRIVED on (email beats chat)."""
    gmail_eid = next((e for e in mission.get("event_ids", [])
                      if e.startswith("gmail:")), "")
    if gmail_eid:
        ev = timeline.get_event(gmail_eid) or {}
        to = (ev.get("sender_key") or mission.get("sender_key", "")).strip()
        if "@" in to:
            try:
                subject = json.loads(ev.get("meta") or "{}").get("subject", "")
            except Exception:
                subject = ""
            if subject and not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            return "send_email", {"to": to,
                                  "subject": subject or f"Re: {mission['task'][:60]}",
                                  "body": draft}
    for eid in reversed(mission.get("event_ids", [])):
        if eid.startswith(("gchat_dm:", "gchat_space:")):
            ev = timeline.get_event(eid) or {}
            space = ev.get("channel", "")
            if space:
                return "send_gchat", {"space": space, "message": draft}
    return None


async def _deliver(mission: dict, sections: dict, broker, router) -> None:
    evidence_bits = []

    report = [f"✅ Done: {mission['task'][:150]}"]
    if sections.get("RESULT"):
        report.append(sections["RESULT"][:900])
    if sections.get("EVIDENCE"):
        report.append("Evidence:\n" + sections["EVIDENCE"][:500])
    files = [f for f in (sections.get("FILES", "").splitlines())
             if f.strip() and f.strip().upper() != "NONE"]
    if files:
        report.append("Files: " + ", ".join(f.strip() for f in files[:6]))
    if mission.get("branch"):
        report.append(f"Branch: {mission['branch']} (worktree: {mission.get('workspace','')})")

    # Code missions with real commits → propose the PR (broker pushes on approve).
    if mission.get("branch") and mission.get("repo"):
        sha = _head_commit(mission.get("workspace", ""))
        if sha:
            evidence_bits.append(f"commit:{sha[:10]}")
            try:
                owner, name = mission["repo"].split("/", 1)
                pr = await broker.propose(
                    "create_pr",
                    f"PR for mission: {mission['task'][:80]}",
                    {"owner": owner, "repo": name, "head": mission["branch"],
                     "base": "main", "title": mission["task"][:80],
                     "body": (sections.get("RESULT", "") + "\n\n"
                              + sections.get("EVIDENCE", ""))[:2000],
                     "local_dir": mission.get("workspace", "")},
                    f"Mission {mission['id']}: work is committed on {mission['branch']}; "
                    "approve to push + open the PR.",
                    project=mission["repo"], source="mission")
                evidence_bits.append(f"pr_action:{(pr or {}).get('action_id', pr.get('id', ''))}"
                                     if isinstance(pr, dict) else "pr_proposed")
            except Exception as e:
                logger.warning("mission %s PR proposal failed: %s", mission["id"], e)

    # The draft reply → ONE approval tap. Recipient comes from Python-held
    # event metadata, so injected text can't redirect it.
    draft = sections.get("DRAFT_REPLY", "")
    if draft and draft.upper() != "NONE":
        addressed = _reply_details(mission, draft)
        if addressed:
            action_type, details = addressed
            try:
                res = await broker.propose(
                    action_type,
                    f"Reply to {mission.get('sender','?')} — task done: {mission['task'][:60]}",
                    details,
                    f"Mission {mission['id']} finished; this is the reply draft. "
                    "The work is already done — approving only sends the message.",
                    project="missions", source="mission",
                    criticality_hint="critical")
                aid = (res or {}).get("action_id") or (res or {}).get("id", "")
                if aid:
                    evidence_bits.append(f"draft_action:{aid}")
                report.append(f"📨 Draft reply to {mission.get('sender','?')} is one tap away "
                              "(approval card sent).")
            except Exception as e:
                logger.warning("mission %s draft proposal failed: %s", mission["id"], e)
                report.append("Draft reply below (proposal failed, copy-paste if useful):\n"
                              + draft[:800])

    mission["status"] = "done"
    mission["evidence"] = "; ".join(evidence_bits)
    mission["finished"] = kv.now_iso()
    _save(mission)
    _record_ledger(mission, status="done",
                   evidence=mission["evidence"] or "delivered:report")
    await router.broadcast("\n\n".join(report))


def _head_commit(workspace: str) -> str:
    """SHA of a commit the mission actually made (empty if none) — the
    'verify a real commit exists' rule from the github_autopilot RCA."""
    import subprocess
    if not workspace:
        return ""
    try:
        base = subprocess.run(["git", "-C", workspace, "merge-base", "HEAD", "origin/HEAD"],
                              capture_output=True, text=True, timeout=15)
        head = subprocess.run(["git", "-C", workspace, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        if head.returncode != 0:
            return ""
        sha = head.stdout.strip()
        if base.returncode == 0 and base.stdout.strip() == sha:
            return ""          # no new commits beyond the base
        return sha
    except Exception:
        return ""


def status_line() -> str:
    missions = kv.get("missions", {}) or {}
    if not missions:
        return "no missions yet"
    recent_missions = sorted(missions.values(), key=lambda m: m.get("created", ""))[-5:]
    return " · ".join(f"{m['status']}: {m['task'][:40]}" for m in recent_missions)
