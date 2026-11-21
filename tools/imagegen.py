"""Image generation for chat — generate a picture and hand back a path to send.

The owner's ask: "the agent should be smart — it can generate an image and share in chat,
for something like daily-update messages to reduce the text load (not every time), it
should select it smartly."

So this is a TOOL the thinking agent calls when a visual genuinely beats a wall of text
(a daily-digest card, a simple chart, a diagram) — NOT on every reply. The agent decides
when (guided by the image-in-chat skill); this module just does the generation reliably
and returns a path the agent then emits as `SENDIMAGE:/path` (the chat core sends it).

Reuses brain.providers.generate_image (Gemini nano-banana, cheapest). Writes to a temp
file in the home tree so the guarded sender (_send_image) accepts it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

logger = logging.getLogger("imagegen")


def _safe_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "img").lower()).strip("-")[:40] or "img"


async def generate(prompt: str, *, name: str = "") -> dict:
    """Generate an image from a text prompt. Returns {path} or {error}.
    The path is a temp .png in the home tree (so the chat file-guard accepts it)."""
    from brain import providers
    slug = _safe_slug(name or prompt)
    # temp file under HOME (the send-guard only allows home-tree / system-temp paths)
    d = os.path.join(os.path.expanduser("~"), ".sentinel", "images")
    os.makedirs(d, exist_ok=True)
    # deterministic-ish name without Date/random (both unavailable in some contexts):
    out = os.path.join(d, f"{slug}-{abs(hash(prompt)) % 100000}.png")
    res = await providers.generate_image(prompt, out)
    if res.get("path") and os.path.isfile(res["path"]):
        return {"path": res["path"]}
    return {"error": res.get("error", "image generation failed")}


async def daily_card(title: str, lines: list[str], *, accent: str = "#4F46E5") -> dict:
    """Render a clean daily-update CARD image (the canonical 'reduce text load' use).
    Builds a design-forward prompt so the output looks like a polished status card, not
    AI slop — structured layout, high contrast, legible text."""
    body = "\n".join(f"• {l}" for l in lines[:8])
    prompt = (
        "A clean, modern minimalist status-card graphic (like a polished mobile app "
        "notification card), 1080x1080, soft neutral background, generous margins. "
        f"Accent color {accent}. Bold title at top: \"{title}\". Below it, a tidy "
        f"vertical list of short status lines, each with a small dot bullet:\n{body}\n"
        "Crisp sans-serif typography, high legibility, plenty of whitespace, no "
        "photorealistic clutter, no gibberish text — render the given text accurately. "
        "Flat design, subtle drop shadow on the card.")
    return await generate(prompt, name=f"card-{_safe_slug(title)}")


def _cli():
    """Allow the agent to call this from Bash: `python -m tools.imagegen "<prompt>"`
    prints the path (which the agent then emits as SENDIMAGE:)."""
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m tools.imagegen '<prompt>'"); return 1
    res = asyncio.run(generate(sys.argv[1]))
    if res.get("path"):
        print(res["path"]); return 0
    print("ERROR:", res.get("error")); return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
