# Chatting with Sentinel

Message `your Sentinel bot` on Telegram like a person. **It remembers the
conversation** — follow-ups like "reply to the second one" or "do that" work.
Also reachable from Google Chat: `@sentinel <ask>` in any of your DMs (only
YOU can trigger it — sender identity is verified).

## Commands (registered in the Telegram menu)

| Command | What it does |
|---------|--------------|
| `/newchat` | fresh conversation (context cleared) |
| `/status` · `/report` | done / pending digest (12h / 24h) |
| `/tasks` | pending approvals + running work |
| `/brief` | what you missed (email, chat, calendar) |
| `/memory` | what it knows about you (facts, corrections, preferences) |
| `/models` | model-race leaderboard with evidence |
| `/insights` | work insights it has collected |
| `/mute 2h` · `/unmute` | pause / resume proactive pings (approvals still come) |
| `/help` | everything you can say |

## Things you can just say

- `check my email` → unread list; then `reply to the 2nd one: <msg>` (uses context)
- `send me Desktop/notes.md` → uploads the file (fuzzy-finds by name too)
- `research <question>` → deep web research with sources
- `fix issue 3 in owner/repo` → isolated worktree → tests → PR approval
- `race <task>` → runs 2–3 models in parallel, judged both orders, evidence logged
- `remember <fact>` → permanent memory
- `less of this` → tunes down whatever proactive ping you just got
- `ignore space X` / `X is work` → relevance filter for briefings/triage
- `watch space X` → GChat bridge watches that group space for your `@sentinel` triggers
- anything else → reasoning agent WITH conversation context + memory + ambient
  work signals (it knows what you're working on)

## What runs by itself (daemon internal cron)

- **Heartbeat every 3h** (07:30–23:30): Needs-you → Done → Watching. Skips when
  there's nothing — silence is health.
- **Proactive engine every 30 min**: meeting prep 45–90 min before events,
  urgent-email flags, stale-approval reminders. Hard budget 3 interrupts/day,
  4h per-category cooldown, 48h dedupe, chief-of-staff gate. Auto-tunes from
  your "less of this".
- **Autonomous work every 2h** (idle-gated): picks open issues on watched repos,
  fixes, tests, proposes PRs.
- **6:05 AM market briefing**: AI/tech developments with images + why-it-matters
  to you; novelty-filtered against everything already sent. May propose (max
  2/week, harsh 8/10 gate, critical approval) a TI ENGG post.
- **3:30 AM memory consolidation**: dream routine (MEMORY.md rewrite), knowledge base
  (DOK knowledge compression), fact decay to archive, conversation trim.
- **Idle experiments** (max 2/day): model races on fixture tasks → leaderboard.

## Formatting & voice

Agents write markdown; every channel renders natively (Telegram HTML, WhatsApp
`*bold*`, GChat markup, email HTML) — you never see raw `**` again. Voice rules
live in `channels/formatting.py::VOICE` + `.claude/skills/chat-voice/`.

## Safety (unchanged)

Reads are free. ANY send-as-you (email, gchat, PR, post) is a broker approval —
`send_email`/`send_gchat` are critical (never fire on timeout). The GChat bridge
replies only to verified-you triggers; group replies are mirrored to Telegram.
Corrections ("that was wrong…") → RCA + never-repeat rule + permanent memory.
