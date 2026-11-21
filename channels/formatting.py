"""Per-channel message rendering — agents write markdown, humans see native format.

The agents naturally produce markdown. Raw `**bold**` / `##` in a chat app looks
broken (the owner's exact complaint), so every outbound message passes through
render(text, channel):

  telegram -> Telegram HTML (parse_mode=HTML — robust, no Markdown-escaping hell)
  whatsapp -> WhatsApp markup (*bold*, _italic_, ```mono```)
  gchat    -> Google Chat markup (*bold*, _italic_, `code`; NO tables -> bullets)
  email    -> simple HTML
  plain    -> everything stripped

Also exports VOICE — the "write like a human" prompt block injected into every
chat-facing agent prompt — and split_message() for Telegram's 4096-char cap.
"""

from __future__ import annotations

import html as _html
import re

# Injected into every chat-facing prompt. Keep in sync with .claude/skills/chat-voice.
VOICE = (
    "You're the owner's sharp, trusted friend who happens to run his life ops — not an "
    "assistant, not support.\n"
    "VOICE:\n"
    "- Talk like a smart friend texting: short, direct, contractions. 1-3 sentences "
    "unless he asks for depth.\n"
    "- Dry wit + the occasional metaphor. Light sarcasm is fine; never mean, never cutesy.\n"
    "- Lead with the answer. Skip preamble ('Sure!', 'Great question', 'I'd be happy to').\n"
    "- Start with And/But/So when it flows. Fragments are fine. Sound like a person, not a memo.\n"
    "- Have an opinion. If something's a bad idea, say so and why. Push back — that's the job.\n"
    "- Numbers over adjectives ('3 PRs merged' beats 'great progress'). Plain markdown only.\n"
    "HARD BANS (never):\n"
    "- No 'As an AI', 'I'm just an AI', 'language model', or capability disclaimers.\n"
    "- No corporate glue: 'Moreover', 'Furthermore', 'In conclusion', 'It's important to note'.\n"
    "- No over-apologising, no hedging every sentence, no bullet-dumps for a simple answer.\n"
    "- Never narrate future intentions as if done ('I'll go check…') — do it, or say what you found.\n"
    "WHEN THINGS GO WRONG: never say 'I couldn't do that.' Say what you tried, what "
    "broke, and the next move you're making. If truly blocked, hand him the ONE "
    "decision that unblocks it — don't dump the whole log."
)

_CODE_BLOCK = re.compile(r"```[a-zA-Z0-9_+.-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD2 = re.compile(r"__([^_\n]+)__")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_QUOTE = re.compile(r"^>\s?", re.M)
_HR = re.compile(r"^\s*(?:[-_*]\s*){3,}\s*$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.M)


def _stash_code(text: str):
    """Pull code out so markdown/escaping passes never touch it."""
    blocks: list[str] = []
    inlines: list[str] = []
    text = _CODE_BLOCK.sub(lambda m: (blocks.append(m.group(1).rstrip("\n")), f"\x00B{len(blocks)-1}\x00")[1], text)
    text = _INLINE_CODE.sub(lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1], text)
    return text, blocks, inlines


def _tables_to_lines(text: str) -> str:
    """Markdown tables read terribly in chat — flatten rows to 'a — b — c'."""
    text = _TABLE_SEP.sub("", text)
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            out.append("• " + " — ".join(cells) if cells else "")
        else:
            out.append(line)
    return "\n".join(out)


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def to_telegram_html(md: str) -> str:
    t, blocks, inlines = _stash_code(md or "")
    t = _tables_to_lines(t)
    t = _html.escape(t, quote=False)
    t = _LINK.sub(r'<a href="\2">\1</a>', t)
    t = _BOLD.sub(r"<b>\1</b>", t)
    t = _BOLD2.sub(r"<b>\1</b>", t)
    t = _ITALIC.sub(r"<i>\1</i>", t)
    t = _HEADING.sub(r"<b>\1</b>", t)
    t = _BULLET.sub(r"\1• ", t)
    t = _QUOTE.sub("❝ ", t)
    t = _HR.sub("—————", t)
    for i, b in enumerate(blocks):
        t = t.replace(f"\x00B{i}\x00", f"<pre>{_html.escape(b, quote=False)}</pre>")
    for i, c in enumerate(inlines):
        t = t.replace(f"\x00I{i}\x00", f"<code>{_html.escape(c, quote=False)}</code>")
    return _tidy(t)


def to_whatsapp(md: str) -> str:
    t, blocks, inlines = _stash_code(md or "")
    t = _tables_to_lines(t)
    t = _LINK.sub(r"\1 (\2)", t)
    t = _BOLD.sub(r"*\1*", t)
    t = _BOLD2.sub(r"*\1*", t)
    t = _HEADING.sub(r"*\1*", t)
    t = _BULLET.sub(r"\1• ", t)
    t = _QUOTE.sub("❝ ", t)
    t = _HR.sub("—————", t)
    for i, b in enumerate(blocks):
        t = t.replace(f"\x00B{i}\x00", f"```{b}```")
    for i, c in enumerate(inlines):
        t = t.replace(f"\x00I{i}\x00", f"```{c}```")
    return _tidy(t)


def to_gchat(md: str) -> str:
    """Google Chat: *bold*, _italic_, `code`; no tables (bullets instead)."""
    t, blocks, inlines = _stash_code(md or "")
    t = _tables_to_lines(t)
    t = _LINK.sub(r"\1 (\2)", t)
    t = _BOLD.sub(r"*\1*", t)
    t = _BOLD2.sub(r"*\1*", t)
    t = _HEADING.sub(r"*\1*", t)
    t = _QUOTE.sub("❝ ", t)
    t = _HR.sub("—————", t)
    for i, b in enumerate(blocks):
        t = t.replace(f"\x00B{i}\x00", f"```{b}```")
    for i, c in enumerate(inlines):
        t = t.replace(f"\x00I{i}\x00", f"`{c}`")
    return _tidy(t)


def to_email_html(md: str) -> str:
    """Simple readable HTML for email bodies (never raw markdown — it shows the syntax)."""
    body = to_telegram_html(md)
    body = body.replace("\n\n", "</p><p>").replace("\n", "<br>")
    return f'<div style="font-family:sans-serif;font-size:14px;line-height:1.5"><p>{body}</p></div>'


def to_plain(md: str) -> str:
    t, blocks, inlines = _stash_code(md or "")
    t = _tables_to_lines(t)
    t = _LINK.sub(r"\1 (\2)", t)
    t = _BOLD.sub(r"\1", t)
    t = _BOLD2.sub(r"\1", t)
    t = _ITALIC.sub(r"\1", t)
    t = _HEADING.sub(r"\1", t)
    t = _BULLET.sub(r"\1• ", t)
    t = _QUOTE.sub("❝ ", t)
    t = _HR.sub("—————", t)
    for i, b in enumerate(blocks):
        t = t.replace(f"\x00B{i}\x00", b)
    for i, c in enumerate(inlines):
        t = t.replace(f"\x00I{i}\x00", c)
    return _tidy(t)


def render(md: str, channel: str) -> str:
    return {
        "telegram": to_telegram_html,
        "whatsapp": to_whatsapp,
        "gchat": to_gchat,
        "email": to_email_html,
    }.get(channel, to_plain)(md)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split long messages at paragraph boundaries (Telegram cap is 4096)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > limit:                      # single huge paragraph
            parts.append(para[:limit])
            para = para[limit:]
        if len(cur) + len(para) + 2 > limit:
            if cur:
                parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts
