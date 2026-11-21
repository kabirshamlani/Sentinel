"""Rubric loader — the per-agent "definition of good", validated at load time.

Rubrics live in coaching/rubrics/*.yaml, in the repo, under git — changing the
definition of good is itself a reviewable diff (and the RCA class `spec-wrong`
targets exactly these files, via PR only).

Schema (validated here; unknown check names fail loudly):

  agent: code-bot                  # canonical name
  aliases: [github_autopilot]      # episode agent-field values that map here
  version: 1
  unit: [files that make up this coachable unit]
  role: one-line purpose
  definition_of_good: prose        # what a good output looks like — for humans
  episode_checks:                  # mechanical, binary, per-episode
    - check: nonempty_output
      params: {min_chars: 10}
      severity: high               # high | medium | low
  wiring_checks:                   # static repo checks, once per cycle
    - check: agent_md_exists
      params: {slug: code-bot}
  judged_dimensions:               # LLM-judged, advisory, sampled
    - name: minimal-diff
      question: ...
      anchors: {1: bad, 3: mixed, 5: good}
  sample_judge_per_cycle: 2
  budget: {max_s: 900, max_usd: 2.0}
  min_occurrences: 2               # failures below this = transient (skip-if-nothing)
  non_goals: prose                 # so 'did the spec, spec was wrong' is classifiable
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from coaching.checks import EPISODE_CHECKS, WIRING_CHECKS

logger = logging.getLogger("coach.rubrics")

RUBRICS_DIR = Path(__file__).resolve().parent / "rubrics"

_REQUIRED = ("agent", "version", "role", "definition_of_good")


class RubricError(ValueError):
    pass


def _validate(rubric: dict, path: Path) -> None:
    for key in _REQUIRED:
        if not rubric.get(key):
            raise RubricError(f"{path.name}: missing required field '{key}'")
    for item in rubric.get("episode_checks", []) or []:
        name = (item or {}).get("check", "")
        if name not in EPISODE_CHECKS:
            raise RubricError(f"{path.name}: unknown episode check '{name}'")
        sev = item.get("severity", "medium")
        if sev not in ("high", "medium", "low"):
            raise RubricError(f"{path.name}: bad severity '{sev}' on check '{name}'")
    for item in rubric.get("wiring_checks", []) or []:
        name = (item or {}).get("check", "")
        if name not in WIRING_CHECKS:
            raise RubricError(f"{path.name}: unknown wiring check '{name}'")
    for dim in rubric.get("judged_dimensions", []) or []:
        if not dim.get("name") or not dim.get("question"):
            raise RubricError(f"{path.name}: judged dimension needs name + question")
        anchors = dim.get("anchors") or {}
        if not all(k in anchors for k in (1, 3, 5)):
            raise RubricError(f"{path.name}: dimension '{dim.get('name')}' needs anchors 1/3/5")


def load_rubrics(directory: Path | None = None) -> dict[str, dict]:
    """Load + validate every rubric. Returns {canonical_agent: rubric}.
    A malformed rubric raises — a silently-skipped definition of good would
    make the coach report health it never measured."""
    directory = directory or RUBRICS_DIR
    rubrics: dict[str, dict] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RubricError(f"{path.name}: not a mapping")
        _validate(data, path)
        data["_path"] = str(path)
        rubrics[data["agent"]] = data
    return rubrics


def rubric_for(agent: str, rubrics: dict[str, dict]) -> dict | None:
    """Resolve an episode's agent field to its rubric (canonical name or alias)."""
    if not agent:
        return None
    low = agent.lower()
    direct = rubrics.get(low) or rubrics.get(agent)
    if direct:
        return direct
    for r in rubrics.values():
        aliases = [str(a).lower() for a in (r.get("aliases") or [])]
        if low in aliases:
            return r
    return None
