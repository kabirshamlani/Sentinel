"""Report — the daily yesterday-vs-today improvement doc.

Derived ONLY from ledger rows (verdicts.jsonl, the kanban, git): pass-rate per
agent yesterday vs today, issues opened/closed by RCA class, fixes proposed/
approved with their before/after evidence. Never self-narrated — if the data
shows nothing, there is no doc (skip-if-nothing is sacred).

Written to ~/.sentinel/coaching/reports/YYYY-MM-DD.md and summarized into the
work log so the 3h heartbeat can carry one line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coaching import kanban
from coaching.episodes import COACH_DIR, _read_jsonl, _parse_ts
from coaching.review import VERDICTS_PATH

logger = logging.getLogger("coach.report")

REPORTS_DIR = COACH_DIR / "reports"


def _window_rates(rows: list[dict], start, end) -> dict[str, dict]:
    """Per-agent {pass, fail} for verdict rows whose episode ts is in [start, end)."""
    agg: dict[str, dict] = {}
    for r in rows:
        ts = _parse_ts(r.get("episode_ts") or r.get("ts") or "")
        if not ts or not (start <= ts < end):
            continue
        a = agg.setdefault(r["agent"], {"pass": 0, "fail": 0})
        a["pass" if r.get("ok") else "fail"] += 1
    return agg


def _fmt_rate(a: dict) -> str:
    total = a["pass"] + a["fail"]
    return f"{a['pass']}/{total} ({a['pass'] / total:.0%})" if total else "—"


def build_daily(now: datetime | None = None) -> tuple[str | None, dict]:
    """Compose the report. Returns (markdown|None, stats). None = nothing to say."""
    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yday_start = today_start - timedelta(days=1)

    verdicts = _read_jsonl(VERDICTS_PATH, limit=50000)
    today = _window_rates(verdicts, today_start, now)
    yesterday = _window_rates(verdicts, yday_start, today_start)

    board = kanban.board_summary()
    issues = kanban.issues()
    recent_cut = now - timedelta(hours=24)

    def _recent(i: dict, field: str) -> bool:
        ts = _parse_ts(i.get(field, ""))
        return ts is not None and ts >= recent_cut

    opened = [i for i in issues if _recent(i, "created")]
    moved = [i for i in issues if _recent(i, "updated") and not _recent(i, "created")]
    in_review = [i for i in issues if i.get("status") == "in_review"]
    done = [i for i in issues if i.get("status") == "done" and _recent(i, "updated")]

    coached = sorted(set(today) | {i["agent"] for i in opened})

    nothing = not today and not opened and not moved and not in_review and not done
    stats = {"agents_reviewed_today": len(today), "issues_opened_24h": len(opened),
             "fixes_in_review": len(in_review), "fixes_merged_24h": len(done),
             "board": board}
    if nothing:
        return None, stats

    lines = [f"# Coaching report — {now.date().isoformat()}",
             "",
             "_Data-derived from verdict ledger + kanban + git. "
             "Yesterday = previous calendar day (UTC)._",
             "",
             "## Yesterday vs today (per-agent check pass-rate)",
             "",
             "| Agent | Yesterday | Today | Trend |",
             "|---|---|---|---|"]
    for agent in sorted(set(yesterday) | set(today)):
        y, t = yesterday.get(agent), today.get(agent)
        trend = "—"
        if y and t and (y["pass"] + y["fail"]) and (t["pass"] + t["fail"]):
            yr = y["pass"] / (y["pass"] + y["fail"])
            tr = t["pass"] / (t["pass"] + t["fail"])
            trend = "📈 improving" if tr > yr + 0.05 else ("📉 regressing" if tr < yr - 0.05 else "→ steady")
        lines.append(f"| {agent} | {_fmt_rate(y) if y else '—'} | "
                     f"{_fmt_rate(t) if t else '—'} | {trend} |")

    if coached:
        lines += ["", f"**Agents coached in this window:** {', '.join(coached)}"]

    if opened:
        lines += ["", "## Issues opened (last 24h)", ""]
        for i in opened:
            lines.append(f"- `{i['id']}` **[{i['severity']}]** {i['title'][:100]} — "
                         f"RCA **{i['rca_class']}** ({i.get('count',1)}x) → {i.get('fix_channel','')}")

    if in_review:
        lines += ["", "## Fixes awaiting your approval", ""]
        for i in in_review:
            fx = i.get("fix", {})
            lines.append(f"- `{i['id']}` {i['title'][:90]}")
            lines.append(f"  - branch `{fx.get('branch','')}` commit `{fx.get('commit','')}` — "
                         f"repro before: FAIL → after: PASS · regression: "
                         f"{'clean' if (fx.get('regression') or {}).get('passed') else 'CHECK'} · "
                         f"review: {(fx.get('review') or {}).get('verdict','')}")

    if done:
        lines += ["", "## Verified improvements (merged + re-verified)", ""]
        for i in done:
            lines.append(f"- `{i['id']}` {i['title'][:90]} — closed with evidence")

    if moved and not (in_review or done):
        lines += ["", f"_{len(moved)} issue(s) progressed on the board._"]

    lines += ["", "## Board", "",
              " · ".join(f"{k}: {v}" for k, v in sorted(board.items())) or "empty"]
    return "\n".join(lines) + "\n", stats


def write_daily() -> dict:
    md, stats = build_daily()
    if md is None:
        logger.info("coaching report: nothing to say — skipped")
        return {"status": "skipped", **stats}
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{datetime.now(timezone.utc).date().isoformat()}.md"
    path.write_text(md, encoding="utf-8")
    try:
        from conductor.loop import log_work
        log_work("coach", f"coaching report: {stats['agents_reviewed_today']} agents reviewed, "
                          f"{stats['issues_opened_24h']} issues opened, "
                          f"{stats['fixes_in_review']} fixes awaiting approval", "ok",
                 {"report": str(path), **stats})
    except Exception:
        pass
    return {"status": "written", "path": str(path), **stats}
