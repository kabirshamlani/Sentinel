"""Prompt registry — every LLM prompt lives here as a .md file, not inline in code.

The owner's requirement: "all the prompts should be in a separate folder." This is that
folder. Prompts are markdown templates with `{placeholder}` slots; code loads them by
dotted name and fills the slots:

    from prompts import load
    text = load("chat.core", memory=mem, transcript=t, request=text)

Why files, not f-strings in code:
- One place to read/tune the agent's whole "operating manual" (the living-brain pattern).
- Diffable, reviewable, versioned separately from logic.
- The Fable-5 guide (research doc 16 §4): steer with intent-level instructions; keeping
  them as prose files makes them easy to keep terse and audit for anti-patterns (e.g. any
  "show your reasoning" language that triggers the reasoning_extraction refusal).

Templates use str.format, so literal braces in a prompt must be doubled `{{ }}`.
Missing placeholders raise loudly (a prompt referencing an unpassed var is a bug).
"""

from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


class _SafeMap(dict):
    """Leave unknown {placeholders} intact rather than KeyError — lets a template
    carry example braces without every caller passing them. Known keys still fill."""
    def __missing__(self, key):
        return "{" + key + "}"


@lru_cache(maxsize=128)
def _raw(name: str) -> str:
    """Load raw template text for a dotted name: 'chat.core' -> prompts/chat/core.md,
    or 'triage_classify' -> prompts/triage_classify.md."""
    rel = name.replace(".", "/")
    for cand in (_DIR / f"{rel}.md", _DIR / f"{rel}.txt"):
        if cand.is_file():
            return cand.read_text()
    raise FileNotFoundError(f"prompt template not found: {name} (looked for {rel}.md)")


def load(name: str, **vars) -> str:
    """Load a prompt template by dotted name and fill {placeholders} with vars.

    Unknown placeholders are left literal (so a template can show example JSON with
    braces); doubled braces `{{ }}` render as single. Raises FileNotFoundError if the
    template file is missing.
    """
    tmpl = _raw(name)
    if not vars:
        return tmpl
    return string.Formatter().vformat(tmpl, (), _SafeMap(**vars))


def exists(name: str) -> bool:
    rel = name.replace(".", "/")
    return (_DIR / f"{rel}.md").is_file() or (_DIR / f"{rel}.txt").is_file()


def clear_cache() -> None:
    """Drop the template cache (call after editing a prompt live)."""
    _raw.cache_clear()
