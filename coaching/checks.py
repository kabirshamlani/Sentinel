"""Mechanical checks — the deterministic half of every rubric.

Two kinds:

  * EPISODE checks: `fn(episode, params) -> (ok, evidence)` where ok is
    True/False or None (= not applicable to this episode, e.g. the stream
    didn't record output text). Binary, machine-checkable, no LLM — the
    research rule: prefer deterministic verifiers; LLM judges are advisory.
  * WIRING checks: `fn(params) -> list[finding]` run once per cycle against
    the repo itself (dead agents, missing agent files, schema mismatches).
    A dead capability described as working is worse than an absent one.

Rubrics reference checks by name; the loader validates names against these
registries so a typo'd rubric fails loudly at load time, not silently at
review time.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from config.paths import REPO_ROOT

# ─────────────────────────── episode checks ───────────────────────────

# Output text that means "the model complained about its own input" — a free,
# high-signal marker that the pipeline fed it bad/missing context.
_META_COMPLAINTS = (
    "there's no task", "there is no task", "no task in your message",
    "i don't have access", "i do not have access", "i cannot see",
    "appears to be truncated", "seems to be truncated", "input is truncated",
    "as an ai language model", "no message provided", "nothing to summarize",
    "i'm not able to", "i am not able to",
)

# Endings that are a promise instead of a result (narration-not-execution).
_PROMISE_TAIL = re.compile(
    r"\b(i'?ll (do|get|start|look|check|work)|i will (do|get|start|look|check)|"
    r"let me (start|work|look|check)|give me a (moment|minute)|working on it now)\b",
    re.IGNORECASE)


def _out(episode: dict) -> str:
    return str(episode.get("output") or "")


def _has_output(episode: dict) -> bool:
    return episode.get("fidelity") in ("full", "output-only") and bool(_out(episode).strip())


def nonempty_output(episode: dict, params: dict) -> tuple[bool | None, str]:
    if episode.get("fidelity") not in ("full", "output-only"):
        # outline streams: use the recorded result_length when present
        rl = (episode.get("meta") or {}).get("result_length")
        if rl is None:
            return None, ""
        min_chars = int(params.get("min_chars", 10))
        return rl >= min_chars, f"recorded result_length={rl}"
    text = _out(episode).strip()
    min_chars = int(params.get("min_chars", 10))
    return len(text) >= min_chars, f"output length {len(text)}"


def no_error(episode: dict, params: dict) -> tuple[bool | None, str]:
    if episode.get("success") is False or episode.get("error"):
        return False, f"error: {str(episode.get('error'))[:200]}"
    return True, ""


def success_label_consistent(episode: dict, params: dict) -> tuple[bool | None, str]:
    """success=True must not coexist with garbage output — mislabels poison
    every downstream learner (217/221 rows said success on anything non-empty)."""
    if episode.get("success") is not True:
        return None, ""
    meta = episode.get("meta") or {}
    if meta.get("result_length") == 0:
        return False, "success=True with result_length=0"
    if not _has_output(episode):
        return None, ""
    text = _out(episode).strip().lower()
    if len(text) < int(params.get("min_chars", 20)):
        return False, f"success=True but output is {len(text)} chars"
    for marker in _META_COMPLAINTS:
        if marker in text:
            return False, f"success=True but output says {marker!r}"
    return True, ""


def none_in_target(episode: dict, params: dict) -> tuple[bool | None, str]:
    """A literal 'None' in a task target means a null flowed through
    formatting unchecked (the 'fix #None' class of bug)."""
    hay = f"{episode.get('input','')} {episode.get('error','')} " \
          f"{(episode.get('meta') or {}).get('status','')}"
    for pat in ("#None", "issue-None", "fix #None", "/None ", "'None'"):
        if pat in hay:
            return False, f"literal None in task target: …{pat}…"
    return True, ""


def commit_evidence(episode: dict, params: dict) -> tuple[bool | None, str]:
    """code-bot runs must end in a real commit (structural verification)."""
    err = str(episode.get("error") or "")
    status = str((episode.get("meta") or {}).get("status") or "")
    if "no commit" in err or "commits_ahead=0" in err:
        return False, f"agent produced no commit: {err[:150]}"
    if status in ("fix_failed",):
        return False, f"status={status}"
    if status in ("proposed_pr", "done"):
        return True, f"status={status}"
    return None, ""


def no_meta_complaints(episode: dict, params: dict) -> tuple[bool | None, str]:
    if not _has_output(episode):
        return None, ""
    text = _out(episode).lower()
    for marker in _META_COMPLAINTS:
        if marker in text:
            return False, f"output contains {marker!r} — the pipeline fed bad context"
    return True, ""


def budget_duration(episode: dict, params: dict) -> tuple[bool | None, str]:
    ms = int(episode.get("duration_ms") or 0)
    if ms <= 0:
        return None, ""
    max_ms = int(params.get("max_s", 900)) * 1000
    return ms <= max_ms, f"duration {ms/1000:.0f}s (budget {max_ms/1000:.0f}s)"


def budget_cost(episode: dict, params: dict) -> tuple[bool | None, str]:
    cost = float(episode.get("cost_usd") or 0.0)
    if cost <= 0:
        return None, ""
    max_usd = float(params.get("max_usd", 2.0))
    return cost <= max_usd, f"cost ${cost:.2f} (budget ${max_usd:.2f})"


def output_max_chars(episode: dict, params: dict) -> tuple[bool | None, str]:
    if not _has_output(episode):
        return None, ""
    limit = int(params.get("limit", 4000))
    n = len(_out(episode))
    return n <= limit, f"output {n} chars (cap {limit})"


def assertion(episode: dict, params: dict) -> tuple[bool | None, str]:
    """Generic evolve-style assertion against the output — one shared grammar
    with the eval gate so rubric checks and regression cases stay consistent."""
    if not _has_output(episode):
        return None, ""
    from learning.evolve import check_assertion
    ok, reason = check_assertion(_out(episode), params)
    return ok, reason


def sources_present(episode: dict, params: dict) -> tuple[bool | None, str]:
    if not _has_output(episode):
        return None, ""
    text = _out(episode)
    if re.search(r"https?://", text):
        return True, ""
    return False, "no source URL in a research answer"


def no_placeholder_sources(episode: dict, params: dict) -> tuple[bool | None, str]:
    if not _has_output(episode):
        return None, ""
    text = _out(episode).lower()
    for pat in ("example.com", "[link]", "[source]", "[url]", "placeholder"):
        if pat in text:
            return False, f"placeholder source {pat!r} — fabricated-citation fingerprint"
    return True, ""


def draft_nonempty_or_skip(episode: dict, params: dict) -> tuple[bool | None, str]:
    """A proposed draft must carry content, or decline explicitly with SKIP —
    empty drafts are the 'why am I seeing this every time' junk class."""
    if episode.get("source") != "action_queue":
        return None, ""
    at = (episode.get("meta") or {}).get("action_type", "")
    if at not in ("draft_email", "draft_gchat", "draft_dm"):
        return None, ""
    body = _out(episode).strip()
    if body.upper() == "SKIP":
        return True, "explicit SKIP"
    return bool(body), "empty draft proposed" if not body else ""


def review_not_empty(episode: dict, params: dict) -> tuple[bool | None, str]:
    """An empty review must never count as LGTM."""
    if not _has_output(episode):
        return False, "review produced no text — empty review counted as clean is a defect"
    return True, ""


def no_doom_loop(episode: dict, params: dict) -> tuple[bool | None, str]:
    if not _has_output(episode):
        return None, ""
    lines = [ln.strip() for ln in _out(episode).splitlines() if ln.strip()]
    run, prev = 1, None
    for ln in lines:
        run = run + 1 if ln == prev else 1
        if run >= int(params.get("repeats", 3)) and len(ln) > 15:
            return False, f"line repeated {run}x: {ln[:80]!r}"
        prev = ln
    return True, ""


def answer_not_promise(episode: dict, params: dict) -> tuple[bool | None, str]:
    """Chat answers must end in results, not promises (momentum > tokens;
    'check your last paragraph — if it's a promise, do the work now')."""
    if not _has_output(episode):
        return None, ""
    tail = _out(episode)[-300:]
    if "PROPOSE:" in _out(episode):
        return True, ""
    m = _PROMISE_TAIL.search(tail)
    if m:
        return False, f"ends with a promise: {m.group(0)!r}"
    return True, ""


def no_raw_ids(episode: dict, params: dict) -> tuple[bool | None, str]:
    """Raw `users/<id>` in user-facing text — names are an identity layer."""
    if not _has_output(episode):
        return None, ""
    m = re.search(r"\busers/\d{6,}\b", _out(episode))
    if m:
        return False, f"raw chat id shown to the user: {m.group(0)}"
    return True, ""


EPISODE_CHECKS = {
    "nonempty_output": nonempty_output,
    "no_error": no_error,
    "success_label_consistent": success_label_consistent,
    "none_in_target": none_in_target,
    "commit_evidence": commit_evidence,
    "no_meta_complaints": no_meta_complaints,
    "budget_duration": budget_duration,
    "budget_cost": budget_cost,
    "output_max_chars": output_max_chars,
    "assertion": assertion,
    "sources_present": sources_present,
    "no_placeholder_sources": no_placeholder_sources,
    "draft_nonempty_or_skip": draft_nonempty_or_skip,
    "review_not_empty": review_not_empty,
    "no_doom_loop": no_doom_loop,
    "answer_not_promise": answer_not_promise,
    "no_raw_ids": no_raw_ids,
}


# ─────────────────────────── wiring checks ───────────────────────────

def _grep(pattern: str, *globs: str) -> list[str]:
    """ripgrep the repo; returns matching 'file:line:text' rows (bounded)."""
    cmd = ["grep", "-rn", "-E", pattern]
    for g in globs or ("--include=*.py",):
        cmd.append(g)
    cmd.append(str(REPO_ROOT))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return [ln for ln in res.stdout.splitlines() if "/coaching/" not in ln][:50]
    except Exception:
        return []


def agent_md_exists(params: dict) -> list[dict]:
    """Every agent slug actually passed to `claude --agent` must have a
    definition file — a missing one silently runs an undefined agent."""
    slug = params.get("slug", "")
    findings = []
    md = REPO_ROOT / ".claude" / "agents" / f"{slug}.md"
    if slug and not md.exists():
        callers = _grep(f'agent="{slug}"|agent=\'{slug}\'')
        if callers:
            findings.append({
                "check": "agent_md_exists",
                "summary": f"agent '{slug}' is invoked but .claude/agents/{slug}.md does not exist "
                           f"— `claude --agent {slug}` references a nonexistent agent",
                "evidence": callers[:5],
                "severity": "high",
            })
    return findings


def has_call_site(params: dict) -> list[dict]:
    """An agent .md with no REACHABLE caller is decoration described as
    capability. params.exclude lists file substrings that are registry-only
    mentions (e.g. a SPECIALISTS table whose routing pattern can never fire) —
    the rubric author's judgment, encoded as data."""
    slug = params.get("slug", "")
    if not slug:
        return []
    md = REPO_ROOT / ".claude" / "agents" / f"{slug}.md"
    if not md.exists():
        return []
    excludes = [str(x) for x in (params.get("exclude") or [])]
    hits = _grep(f"{slug}|{slug.replace('-', '_')}")
    real = [h for h in hits
            if "/.claude/agents/" not in h and "test" not in h.lower()
            and not any(x in h for x in excludes)]
    if not real:
        why = " (registry mentions excluded as unreachable)" if excludes else ""
        return [{
            "check": "has_call_site",
            "summary": f"agent '{slug}' has a definition file but no reachable call site{why} — "
                       f"orphaned capability (never runs, cannot be coached from episodes)",
            "evidence": [f".claude/agents/{slug}.md"] + excludes,
            "severity": "medium",
        }]
    return []


def artifact_clean(params: dict) -> list[dict]:
    """A produced artifact (today.md, goals.yaml…) must not contain the model
    complaining about its own input — that is a context-pipeline defect
    captured in the deliverable itself."""
    from config.paths import SENTINEL_HOME
    rel = params.get("path", "")
    if not rel:
        return []
    path = (SENTINEL_HOME / rel) if not rel.startswith("/") else Path(rel)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8").lower()
    except OSError:
        return []
    for marker in _META_COMPLAINTS + ("truncated",):
        if marker in text:
            return [{
                "check": "artifact_clean",
                "summary": f"{path.name} contains {marker!r} — the model's complaint about bad "
                           f"input shipped inside the artifact",
                "evidence": [str(path)],
                "severity": "medium",
            }]
    return []


def journal_field_mismatch(params: dict) -> list[dict]:
    """error_journal CONSUMERS must read a field the journal writes.
    Reading only e.get('error') where the journal writes 'error_message' makes
    the failure-clustering loop run on empty strings.

    Precision matters: a repo-wide grep for `.get("error")` also hits (a) the
    already-fixed sites that read 'error_message' first and keep 'error' as a
    legacy fallback, and (b) tool-result dicts that own their own {'error':...}
    shape and never touch the journal. Flagging those makes the check
    unsatisfiable and re-raises this ticket forever (the ci-03096f42-r1 loop).
    So: flag ONLY a line that reads `.get("error")` WITHOUT also reading
    'error_message', AND only in files that actually consume the error journal."""
    findings = []
    # a genuine consumer imports/reads the error journal
    consumer_files = set()
    for hit in _grep(r"error_journal|get_recent_errors|get_all_entries|ERROR_JOURNAL"):
        path = hit.split(":", 1)[0]
        if path.endswith(".py"):
            consumer_files.add(path)

    consumer_files = {p for p in consumer_files if "/tests/" not in p}
    suspicious = []
    for line in _grep(r"\.get\(['\"]error['\"]"):
        try:
            path, _lineno, code = line.split(":", 2)
        except ValueError:
            continue
        if path.endswith(("error_journal.py", "checks.py")) or "/tests/" in path:
            continue
        if path not in consumer_files:
            continue                              # not an error-journal consumer
        if "error_message" in line:
            continue                              # already reads the right field first
        # code, not a comment/docstring mentioning the pattern in prose
        stripped = code.strip()
        if stripped.startswith("#") or stripped.startswith(('"', "'", "*", "--")):
            continue
        suspicious.append(line)

    if suspicious:
        findings.append({
            "check": "journal_field_mismatch",
            "summary": "error_journal consumer reads e.get('error') but the journal writes "
                       "'error_message' — failure clustering runs on empty strings",
            "evidence": suspicious[:10],
            "severity": "high",
        })
    return findings


def slug_map_covers_agent(params: dict) -> list[dict]:
    """A vertical passing agent='<name>' must resolve to a real slug via
    claude_runner._AGENT_SLUG_MAP (or be 'conductor')."""
    name = params.get("agent_name", "")
    if not name:
        return []
    runner = (REPO_ROOT / "brain" / "claude_runner.py").read_text(encoding="utf-8")
    agents_dir = REPO_ROOT / ".claude" / "agents"
    key = name.lower().replace("-", "_")
    if f'"{key}"' in runner.split("_AGENT_SLUG_MAP")[-1][:2000]:
        return []
    if name in ("conductor", "default") or (agents_dir / f"{name}.md").exists():
        return []
    callers = _grep(f'agent="{name}"|agent=\'{name}\'')
    if not callers:
        return []
    return [{
        "check": "slug_map_covers_agent",
        "summary": f"verticals invoke agent='{name}' but it is neither in _AGENT_SLUG_MAP nor "
                   f".claude/agents/ — the CLI gets --agent {name} which doesn't exist",
        "evidence": callers[:5],
        "severity": "high",
    }]


WIRING_CHECKS = {
    "agent_md_exists": agent_md_exists,
    "has_call_site": has_call_site,
    "journal_field_mismatch": journal_field_mismatch,
    "slug_map_covers_agent": slug_map_covers_agent,
    "artifact_clean": artifact_clean,
}
