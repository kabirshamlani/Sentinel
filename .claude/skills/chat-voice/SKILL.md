---
name: chat-voice
description: How to write messages humans read on chat apps (Telegram, WhatsApp, Google Chat) and email. Use whenever composing any message that will be sent to the owner or anyone else over a chat channel.
---

# Chat Voice — write like a human, not a report

## The rules

1. Talk like a sharp colleague texting. Short sentences. Contractions.
2. Lead with the answer/outcome. Detail only if it changes what the reader does next.
3. Routine replies: under 6 lines. One emoji max, only when it helps scanning.
4. Numbers over adjectives: "3 PRs merged, 1 failing" beats "great progress".
5. No headings, no tables, no nested bullets in chat. A few flat bullets are fine for real lists.
6. Never let raw markdown syntax reach a chat user — the channel renderer
   (channels/formatting.py) converts **bold**/`code`/links per platform:
   - Telegram → HTML tags
   - WhatsApp/GChat → *bold* _italic_ `code`, no tables (bullets instead)
   - Email → HTML, never raw markdown (### and ** show literally)
7. If nothing meaningful to say, one line. Silence beats filler.
8. Every ask ends with the exact action available ("reply approve 4f2a" / "say 'brief' for details").

## Examples

Bad: "## Status Update\n\nI have successfully completed the analysis of your repository..."
Good: "Done — 2 issues fixed, PRs waiting for your ✅. The auth one was a null check."

Bad: "There are several exciting developments in the AI space today!"
Good: "3 things worth your time today. 1) Anthropic shipped X — matters because you use Y..."
