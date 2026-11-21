# Sentinel

[![Sentinel in 21 seconds](assets/sentinel.gif)](assets/sentinel.mp4)

*Click the preview for the full video with sound.*

A personal autonomous agent army. It perceives your work (GitHub issues/Kanban,
Gmail, Google Chat, Calendar, meeting transcripts), reasons with Claude Code,
proposes actions, and — with your approval — acts. You stay the observer/approver;
routine work runs on its own, and the system learns from every correction.

## How it works

```
launchd (15-min cycle + 3-hourly report)
   │  idle-gate (heavy work only when the Mac is free)
   ▼
CONDUCTOR ── perceive ─▶ reason (Claude Code) ─▶ propose ─▶ ACT ─▶ report ─▶ learn
(no LLM)     read-only    coding/triage agents    broker             │        │
                                                     │               │        └─ error journal + skillbook (auto-promote)
                                                     ▼               ▼
                                          APPROVAL BROKER      Telegram + WhatsApp
                                          R / A / X tiers      (approve/reject, first wins)
```

- **Conductor** (`conductor/`) owns the loop deterministically — no LLM at this layer.
- **Claude Code** (`brain/claude_runner.py`, forked & hardened) does the reasoning
  in a per-tier permission sandbox (no `--dangerously-skip-permissions`).
- **Approval broker** (`approval/`) enforces your rules:
  - **Tier R** read/report → runs immediately.
  - **Tier A** act → needs approval. **Routine**: no reply in 30 min → best-effort
    proceed + "window expired" report. **Critical/irreversible**: hold forever.
  - **Tier X** earned → action-types with a proven success ledger auto-run.
  - **Per-project one-time approval**: approve once for a project → future tasks
    on it are pre-approved.
- **Channels** (`channels/`): Telegram (primary, buttons) + WhatsApp via OpenClaw.
- **Learning** (`learning/`): error journal + skillbook (repeated failures →
  rules), **RCA** ("you made a mistake" → root-cause → never repeat), and an
  eval-gated **self-evolving** loop (research → verify → propose diff → approve).
- **Discovery** (`discovery/`): finds & vets MCP servers / skills; malicious ones
  refused, everything else approval-gated (never auto-installed).

## Verticals (`verticals/`)
- **github_autopilot** — issue/Kanban → fix on a branch → propose PR (agent is
  structurally blocked from pushing/opening PRs; only the broker does, after you approve).
- **inbox_triage** — unread Gmail/GChat → draft reply in your voice (draft only).
- **meeting_intel** — pre-meeting prep notes + post-meeting analysis from transcripts.
- **reporter** — daily + 3-hourly done/pending/failed digest.

## Quick start

Requires macOS, Python 3.11+, and the [Claude Code](https://claude.com/claude-code) CLI signed in.

```bash
git clone https://github.com/kabirshamlani/Sentinel.git
cd Sentinel
bash deploy/install.sh    # venv + deps + launchd jobs
```

Then give it your own accounts. Secrets go in the macOS keychain, never in files
(`security add-generic-password -s NAME -w VALUE -U`); [`.env.example`](.env.example)
lists every name the system looks for.

| Setting | What it is |
|---|---|
| `SENTINEL_TELEGRAM_TOKEN`, `SENTINEL_TELEGRAM_CHAT_ID` | Your Telegram bot (from @BotFather) and your chat id. Approvals arrive here. |
| `GITHUB_TOKEN` | A personal token scoped to the repos Sentinel may watch. |
| `SENTINEL_GOOGLE_ACCOUNT` | The Google account your `gog` CLI is authorized for (Gmail, Chat, Calendar). |
| `SENTINEL_HOME` | Runtime state and logs. Defaults to `~/.sentinel`. |

Run the tests with `python -m pytest tests --ignore=tests/e2e_live.py`.
More detail: [`docs/CHAT.md`](docs/CHAT.md), [`docs/TOOLS.md`](docs/TOOLS.md), [`docs/COACHING.md`](docs/COACHING.md).

## Safety model (why you can trust it with access)
- Nothing binds to a public port; secrets live in the macOS keychain, never in files.
- **Rule of Two**: no single agent run combines untrusted input + credentials +
  unconstrained egress. Perception is read-only; mutations are a separate gated step.
- A **deny-guard hook** blocks `git push`, `gh pr create`, `aws … delete`, force-push,
  secret reads, and destructive shell — even if a prompt tries.
- **git is the rollback substrate**; self-modification is always critical + PR'd, never auto-applied.

## License

[MIT](LICENSE)
