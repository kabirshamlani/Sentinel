"""Model router — task -> tier -> concrete model id, cost-aware.

Merges the two conflicting routers from the old repo into one:
- policy layer: task_type -> tier (from model_selector.py)
- complexity layer: 1-10 score -> tier (from brain/model_router.py)
- resolution layer: tier -> concrete model id, honoring the TrueFoundry
  gateway slugs when routed through it (from truefoundry.py)

Model IDs are current as of July 2026 (see agent-army-research/01). When run
through Claude Code, returning `None` for the model lets the CLI use its own
configured default (the safest option on a machine already wired to the TFY
proxy). The IDs here are used for direct SDK/API calls and cost estimation.

The self-evolving loop (learning/evolve.py) proposes edits to THIS file's
TIER_MODELS table when a better/cheaper model ships — gated by approval.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from typing import Literal, Optional

logger = logging.getLogger("model-router")

Tier = Literal["quick", "standard", "deep", "frontier", "vision", "bulk", "embedding"]


@dataclass
class ModelProfile:
    tier: Tier
    provider: str            # anthropic | openai | google
    model: str               # direct API id
    tfy_slug: str            # TrueFoundry gateway slug (claude-group/...)
    max_tokens: int
    cost_in: float           # $ / 1K input tokens
    cost_out: float          # $ / 1K output tokens
    supports_vision: bool = False


# Current model lineup (July 2026). $/1K = ($/MTok)/1000.
TIER_MODELS: dict[Tier, ModelProfile] = {
    # Cheap classify/triage/extract/heartbeat — Haiku 4.5
    "quick": ModelProfile(
        tier="quick", provider="anthropic",
        model="claude-haiku-4-5", tfy_slug="claude-group/claude-haiku-4-5-20251001",
        max_tokens=4000, cost_in=0.001, cost_out=0.005,
    ),
    # Everyday coding / drafting / analysis — Sonnet 5 (intro pricing)
    "standard": ModelProfile(
        tier="standard", provider="anthropic",
        model="claude-sonnet-5", tfy_slug="claude-group/claude-sonnet-5",
        max_tokens=16000, cost_in=0.002, cost_out=0.010,
    ),
    # Orchestration / planning / hard reasoning — Opus 5 (Claude 5 family,
    # July 2026; CLI-probed OK 2026-07-29; supersedes Opus 4.8 at this tier)
    "deep": ModelProfile(
        tier="deep", provider="anthropic",
        model="claude-opus-5", tfy_slug="claude-group/claude-opus-5",
        max_tokens=32000, cost_in=0.005, cost_out=0.025,
    ),
    # Hardest coding / overnight autonomous / escalation — Fable 5
    "frontier": ModelProfile(
        tier="frontier", provider="anthropic",
        model="claude-fable-5", tfy_slug="claude-group/claude-fable-5",
        max_tokens=32000, cost_in=0.010, cost_out=0.050,
    ),
    # Dense-document / degraded-image vision — Fable 5 (best-in-class GDP.pdf)
    "vision": ModelProfile(
        tier="vision", provider="anthropic",
        model="claude-fable-5", tfy_slug="claude-group/claude-fable-5",
        max_tokens=8000, cost_in=0.010, cost_out=0.050, supports_vision=True,
    ),
    # Bulk summarization (long ctx, cheap, fast) — Gemini 3.5 Flash
    "bulk": ModelProfile(
        tier="bulk", provider="google",
        model="gemini-3.5-flash", tfy_slug="",
        max_tokens=16000, cost_in=0.0015, cost_out=0.009,
    ),
    "embedding": ModelProfile(
        tier="embedding", provider="openai",
        model="text-embedding-3-small", tfy_slug="",
        max_tokens=0, cost_in=0.00002, cost_out=0.0,
    ),
}

# task_type -> tier (policy). Unknown -> "standard".
TASK_TO_TIER: dict[str, Tier] = {
    # quick (Haiku — high-volume structured; error caught by notify-default)
    "classify": "quick", "triage": "quick", "extract": "quick",
    "triage_classify": "quick", "email_count": "quick", "calendar_check": "quick",
    "simple_lookup": "quick", "criticality_check": "quick", "route": "quick",
    # standard (Sonnet — chat planning + drafting + analysis + meeting extraction)
    "code_fix": "standard", "code_review": "standard", "draft_email": "standard",
    "draft_reply": "standard", "cross_reference": "standard", "chat_analysis": "standard",
    "meeting_prep": "standard", "chat": "standard", "meeting_recall": "standard",
    "default": "standard",
    # deep (orchestration / planning / multi-source research synthesis)
    "orchestrate": "deep", "plan": "deep", "strategy_planning": "deep",
    "architecture_review": "deep", "complex_reasoning": "deep", "rca": "deep",
    "research": "deep",
    # frontier (hardest)
    "hard_refactor": "frontier", "overnight_autonomous": "frontier", "multi_repo": "frontier",
    # vision
    "image_analysis": "vision", "screenshot_review": "vision", "video_frame_analysis": "vision",
    # bulk
    "summarize_transcript": "bulk", "summarize_logs": "bulk", "bulk_summary": "bulk",
    # research handled out-of-band by the sherlock skill
}

# complexity score 1-10 -> tier (used by the perceive->reason path when a
# task_type isn't known; mirrors the old brain/model_router behavior).
def tier_for_complexity(score: int) -> Tier:
    if score <= 2:
        return "quick"
    if score <= 6:
        return "standard"
    if score <= 8:
        return "deep"
    return "frontier"


ESCALATION = ["quick", "standard", "deep", "frontier"]


def select_tier(task_type: str = "default", complexity: Optional[int] = None) -> Tier:
    """Pick a tier. task_type wins; else complexity; else standard."""
    if task_type and task_type in TASK_TO_TIER:
        return TASK_TO_TIER[task_type]
    if complexity is not None:
        return tier_for_complexity(complexity)
    return "standard"


def escalate(tier: Tier) -> Tier:
    """Bump one tier up the coding-capability ladder (for retries)."""
    if tier in ESCALATION:
        i = ESCALATION.index(tier)
        return ESCALATION[min(i + 1, len(ESCALATION) - 1)]
    return "deep"


def profile_for(tier: Tier) -> ModelProfile:
    return TIER_MODELS.get(tier, TIER_MODELS["standard"])


def _tfy_enabled() -> bool:
    return os.getenv("TFY_ENABLED", "").lower() in ("1", "true", "yes") or bool(
        os.getenv("TFY_BASE_URL", "")
    )


def resolve_model(tier: Tier, for_cli: bool = True) -> Optional[str]:
    """Concrete model id for a tier.

    for_cli=True (Claude Code path): return None so the CLI uses its own
    configured default/model-mapping (safest on the TFY-wired machine), UNLESS
    we specifically need a non-default tier — then return the TFY slug if the
    gateway is active, else the direct id.
    for_cli=False (direct SDK/API): return the concrete id (TFY slug if gateway).
    """
    p = profile_for(tier)
    if for_cli:
        # Cheap-first (research doc 15): return a CONCRETE slug for every tier so
        # cheap work actually runs cheap instead of defaulting to the CLI's model
        # (which was Opus-class → $1.36/day for triage). Only 'deep' inherits the
        # CLI default (planning/high-stakes wants the strongest model available).
        if tier == "deep":
            return None
        if p.model:
            return p.tfy_slug if (_tfy_enabled() and p.tfy_slug) else p.model
        return None
    if _tfy_enabled() and p.tfy_slug:
        return p.tfy_slug
    return p.model


def select_model(task_type: str = "default", complexity: Optional[int] = None,
                 for_cli: bool = True) -> Optional[str]:
    """Convenience: task/complexity -> concrete model id (or None for CLI default)."""
    return resolve_model(select_tier(task_type, complexity), for_cli=for_cli)


def estimate_cost(input_tokens: int, output_tokens: int, tier: Tier = "standard") -> float:
    p = profile_for(tier)
    return (input_tokens / 1000) * p.cost_in + (output_tokens / 1000) * p.cost_out


def all_profiles() -> dict[str, dict]:
    return {t: asdict(p) for t, p in TIER_MODELS.items()}
