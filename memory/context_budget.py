"""Context budget manager that enforces per-layer token limits."""

from __future__ import annotations

from dataclasses import dataclass, field

LAYER_DEFAULTS = {
    "global": 1500,
    "agent": 2000,
    "session": 1500,
    "task": 1000,
}


@dataclass
class ContextBudget:
    """Manages token budgets across memory layers."""

    total: int = 6000
    allocations: dict[str, int] = field(default_factory=lambda: dict(LAYER_DEFAULTS))

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate: ~4 chars per token."""
        return len(text) // 4

    def truncate_to_budget(self, content: str, layer: str) -> str:
        """Truncate content to fit layer budget. Add overflow note if truncated."""
        budget = self.allocations.get(layer, 1000)
        max_chars = budget * 4
        if len(content) <= max_chars:
            return content
        return content[:max_chars - 80] + "\n\n[... truncated to fit context budget ...]"

    def assemble_context(
        self,
        global_ctx: str,
        agent_ctx: str,
        session_ctx: str,
        task_ctx: str,
    ) -> str:
        """Assemble context from all layers, respecting budgets."""
        parts = []
        for layer_name, content in [
            ("global", global_ctx),
            ("agent", agent_ctx),
            ("session", session_ctx),
            ("task", task_ctx),
        ]:
            if content.strip():
                truncated = self.truncate_to_budget(content, layer_name)
                parts.append(f"## {layer_name.title()} Context\n\n{truncated}")
        assembled = "\n\n---\n\n".join(parts)
        total_chars = self.total * 4
        if len(assembled) > total_chars:
            assembled = assembled[:total_chars - 80] + "\n\n[... context budget exceeded, truncated ...]"
        return assembled

    def remaining(self, layer: str, used_text: str) -> int:
        """Return remaining token budget for a layer."""
        budget = self.allocations.get(layer, 1000)
        return max(0, budget - self.estimate_tokens(used_text))
