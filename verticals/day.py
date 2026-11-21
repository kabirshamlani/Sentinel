"""Daily user-model — "understand my day" (the chief-of-staff core).

Research doc 15 §d (claude-chief-of-staff pattern): the user-model is a SMALL set
of durable files the agent reads every cycle and writes back — not a vector soup:
  goals.yaml  — priorities = SOURCE OF TRUTH; every triage/task decision filters
                through "does this serve a goal?" (seeded by inference, the owner corrects)
  tasks.yaml  — open tasks {task, deadline, source, goal_link, status}
  today.md    — working memory regenerated each morning: today's calendar + meeting
                outcomes owed + open tasks w/ deadlines + what slipped + the ONE
                thing that matters most.

These live under ~/.sentinel/user/. They're injected into the chat planner, triage,
and meeting scoring so the agent is proactive and goal-aware instead of reactive.
"""

from __future__ import annotations

import logging
from datetime import datetime

from coaching.checks import _META_COMPLAINTS
from config.paths import SENTINEL_HOME

logger = logging.getLogger("day")

USER_DIR = SENTINEL_HOME / "user"
GOALS = USER_DIR / "goals.yaml"
TASKS = USER_DIR / "tasks.yaml"
TODAY = USER_DIR / "today.md"


def _ensure():
    USER_DIR.mkdir(parents=True, exist_ok=True)


def goals_text() -> str:
    _ensure()
    if GOALS.exists():
        return GOALS.read_text(encoding="utf-8")[:1500]
    return ""


def tasks_text() -> str:
    _ensure()
    if TASKS.exists():
        return TASKS.read_text(encoding="utf-8")[:1500]
    return ""


def today_text() -> str:
    _ensure()
    if TODAY.exists():
        return TODAY.read_text(encoding="utf-8")[:2000]
    return ""


def context_block(max_chars: int = 1400) -> str:
    """Compact 'who the owner is + what he's doing today' for injection into prompts."""
    parts = []
    g = goals_text()
    if g:
        parts.append("the owner's goals/priorities (filter decisions through these):\n" + g[:600])
    t = today_text()
    if t:
        parts.append("Today:\n" + t[:600])
    tk = tasks_text()
    if tk:
        parts.append("Open tasks:\n" + tk[:400])
    return "\n\n".join(parts)[:max_chars]


def set_goals(text: str) -> None:
    _ensure()
    GOALS.write_text(text, encoding="utf-8")


async def infer_goals() -> str:
    """Seed goals.yaml by inferring from recent meetings + Cursor + email (the owner
    corrects it after). Returns the drafted YAML."""
    from brain import claude_runner
    from tools import observer, gog_workspace as gw
    signals = []
    try:
        ctx = observer.summary_text(max_chars=800)
        if ctx:
            signals.append("Ambient work:\n" + ctx)
    except Exception:
        pass
    try:
        recaps = await gw.search_emails('subject:(Notes OR recap OR "action items") newer_than:7d', 5)
        if recaps:
            signals.append("Recent meeting subjects:\n" +
                           "\n".join("- " + (r.get("subject", "") or "")[:70] for r in recaps))
    except Exception:
        pass
    prompt = (
        "From these signals about the owner's recent work, draft a concise goals.yaml "
        "capturing his current priorities/projects (quarterly + this-week level). "
        "Keep it short — 3-6 goals, each one line. This is a DRAFT he'll correct.\n\n"
        + "\n\n".join(signals) + "\n\n"
        "Return YAML only:\nquarter:\n  - <goal>\nweek:\n  - <focus>\nprojects:\n  - <name>: <one line>")
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             task_type="chat", allowed_tools=[],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    out = out.strip()
    if out and "Usage Policy" not in out:
        set_goals(out)
    return out


async def build_today() -> str:
    """Regenerate today.md: calendar + meetings + open tasks + what matters. Run
    each morning (and on demand). Returns the markdown."""
    from brain import claude_runner
    from tools import gog_workspace as gw
    today = datetime.now().strftime("%A %Y-%m-%d")
    events = []
    try:
        events = await gw.upcoming_events(8)
    except Exception:
        pass
    today_date = datetime.now().strftime("%Y-%m-%d")
    cal_lines = []
    for e in events[:8]:
        e = gw._norm_event(e)
        summary = (e.get("summary", "") or "")[:60]
        start = e.get("start", "") or ""
        if len(start) >= 16 and start[11] == "T":
            when = start[11:16]
        elif len(start) == 10:
            when = "(all day)"
        else:
            when = "(time unknown)"
        label = "" if start[:10] == today_date else " (tomorrow)"
        cal_lines.append(f"- {summary} @ {when}{label}")
    cal = "\n".join(cal_lines) or "(no events)"
    prompt = (
        f"Compose the owner's `today.md` working memory for {today}. Be his chief of "
        "staff: tight, opinionated, lead with what matters.\n\n"
        f"Goals:\n{goals_text() or '(none set)'}\n\nOpen tasks:\n{tasks_text() or '(none)'}\n\n"
        f"Today's calendar:\n{cal}\n\n"
        "Write markdown: **The one thing** (most important today), then today's "
        "meetings (with any prep note), then tasks due, then what slipped. 8-12 "
        "lines max, no fluff. If a day is light, say so.")
    out = ""
    async for ev in claude_runner.run_claude(prompt, agent="conductor", tier="read",
                                             task_type="chat", allowed_tools=[],
                                             model=None, max_turns=0):
        if ev.type == "result":
            out = ev.content or ""
    out = out.strip()
    lowered = out.lower()
    if out and "Usage Policy" not in out and not any(m in lowered for m in _META_COMPLAINTS):
        _ensure()
        TODAY.write_text(f"<!-- {datetime.now().isoformat()} -->\n{out}\n", encoding="utf-8")
    elif out:
        logger.warning("build_today(): rejected output (meta-complaint or policy marker), keeping prior today.md")
    return out


def load_vips() -> set[str]:
    """VIP email addresses parsed from goals/tasks (people the owner works with closely).
    Used by triage to never drop a real colleague. Best-effort."""
    import re
    text = goals_text() + "\n" + tasks_text() + "\n" + today_text()
    return {a.lower() for a in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)}


def add_task(task: str, deadline: str = "", source: str = "chat", goal: str = "") -> None:
    """Append a task (from a meeting/email/chat) to tasks.yaml."""
    _ensure()
    line = f"- task: {task[:120]}\n  deadline: {deadline}\n  source: {source}\n  goal: {goal}\n"
    with open(TASKS, "a", encoding="utf-8") as f:
        f.write(line)
