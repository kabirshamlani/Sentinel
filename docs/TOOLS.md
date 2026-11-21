# Sentinel Tool & Capability Catalog

The complete surface an agent can act through, and **which tier each is safe on**.
This file is the human-readable expression of Sentinel's safety model — if a tool
isn't here, it isn't sanctioned.

## The tiers

| Tier | Meaning | Approval? |
|------|---------|-----------|
| **R** (read) | Perception / read / report. No mutation. | None — runs immediately. |
| **A** (act)  | Edit / write / scoped bash / external mutation. | Every mutation flows through `approval/broker.py`. |

`brain/claude_runner.py` enforces this at the process level:

- `tier="read"` → `--allowedTools Read,Grep,Glob,WebSearch,WebFetch` + `--permission-mode plan`; **no** `.mcp.json` is loaded (perception stays lean and credential-free).
- `tier="act"` → adds `Edit,Write,Bash` + `--permission-mode acceptEdits` + loads `.mcp.json`.
- Both tiers scrub secrets (`GITHUB_TOKEN`, cloud keys, Telegram tokens, …) from the child env; the broker holds credentials and performs the privileged push/PR/send on the agent's behalf.

## The Rule-of-Two invariant

No single agent session may combine all three of:

1. **Untrusted input** — anything browsed/read from the web, issue bodies, emails, chat messages, or arbitrary repo content.
2. **Credentials** — tokens/keys/OAuth.
3. **Unconstrained external action** — push, send, publish, unscoped bash.

**Browsing / web-reading counts as untrusted input.** Therefore any web-touching
or content-ingesting session runs at tier **R** (no credentials, no unconstrained
action), and the trusted broker performs mutations separately. The "untrusted-input
caveat" column below flags every tool that introduces leg #1.

---

## Native Claude Code tools

| Tool | Tier | Purpose | Untrusted-input caveat |
|------|------|---------|------------------------|
| `Read`   | R | Read a file. | Repo content can be attacker-influenced (issue-linked code); it's input, not commands. |
| `Grep`   | R | Search file contents by regex. | — |
| `Glob`   | R | Find files by glob pattern. | — |
| `WebSearch` | R | Search the web from within the agent. | **Yes** — results are untrusted. Read tier only. |
| `WebFetch` | R | Fetch + read a URL from within the agent. | **Yes** — page content is untrusted. Read tier only. |
| `Edit`   | A | Modify a file in place. | Only on act tier (in an isolated worktree for coding). |
| `Write`  | A | Create/overwrite a file. | Act tier only. |
| `Bash`   | A | Run a shell command. | Act tier only; gated by the settings.json PreToolUse deny-guard hook + the `DANGEROUS_COMMANDS` denylist in `tools/system_tools.py`; push/PR are structurally blocked so only the broker mutates remotes. |

## MCP servers (`.mcp.json`) — loaded on **act** tier only

Read/perception deliberately runs without MCP (fewer tools → better, cheaper
reasoning, and no credential-bearing servers next to untrusted input).

| Server | Tier | Purpose | Untrusted-input caveat |
|--------|------|---------|------------------------|
| `serena`     | A | Semantic code navigation / editing for the coding agent. | Operates on repo code (input); no external mutation. |
| `context7`   | A | Live, version-accurate library docs. | Fetches external docs → treat returned text as untrusted; it carries an API key, so act tier + trusted repo task only. |
| `github`     | A | Structured GitHub context (issues, PRs, code) via the GitHub MCP. | Reads attacker-influenceable issue/PR text; the broker (not this) performs the actual push/PR. |
| `fetch`      | A | Generic URL fetch (the MCP web-read path). | **Yes** — fetched content is untrusted. |
| `playwright` | A | Headless browser automation (`--isolated --headless`). | **Yes** — browsed pages are untrusted; isolated profile, no stored creds. |

> Note: the *credential-free* web-read path for R-tier work is the Python
> `tools/web_tools.py` module below (used by `verticals/research_agent.py`), **not**
> these MCP servers.

## Python tool modules (`tools/`)

| Module | Tier | Purpose | Untrusted-input caveat |
|--------|------|---------|------------------------|
| `system_tools` | R + A | Filesystem read (`read_any_file`, `list_directory`, `search_files`, `get_file_info`, `read_image_base64`) and safe read helpers (`which`, `env_summary`, `disk_and_mem`) are **R**; `run_command` is **A**. | `run_command` enforces the `DANGEROUS_COMMANDS` denylist + regex patterns. `env_summary` returns **only non-secret variable NAMES** — never values, and never any name matching KEY/TOKEN/SECRET/PASSWORD (+ wider denylist). |
| `web_tools` | R | Credential-free web layer: `web_search` (DuckDuckGo HTML), `read_url` (readable text + links, ~15k), `screenshot`, `read_many`. Never carries creds; never mutates. | **Yes** — all returned page/search text is untrusted. Designed to feed R-tier sessions only. |
| `browser` | R (read) / A (interact) | Playwright wrapper: `navigate`, `get_page_text`, `web_search`, `take_screenshot` are read-only (R); `fill_form` (submitting a form) is an external action (A). | **Yes** — browsed content is untrusted. Form submission must be an approved act. |
| `vision` | R | Multimodal image/screenshot/video-frame analysis via a vision model. | Image content is untrusted input; analysis only, no mutation. |
| `github_tools` | R (reads) / A (writes) | GitHub REST: `list_issues`, `get_ci_status`, etc. are **R**; branch/PR/comment creation is **A** and only ever invoked by the broker's executor (credentialed, trusted). | Issue/PR bodies are untrusted; the coding agent never holds `GITHUB_TOKEN`. |
| `workspace_tools` | R (reads) / A (writes) | Gmail / Calendar / Drive / Chat. Listing/reading is **R**; sending mail, creating events, posting chat, creating drafts is **A** (broker-gated). | Email/chat content is untrusted input → the drafting session is R-tier with no send capability; the broker sends. |
| `repo_manager` | A | Clone/refresh repos, hand out isolated git worktrees, cleanup. Trusted infrastructure (runs outside the agent session). | — (the agent works *inside* the worktree and is still denied push). |
| `test_runner` | R | Auto-detect + run a repo's test suite; returns pass/fail + output tail (the PR quality gate). | Runs project test code in a worktree; output is informational. |
| `safe_edit` | A | Never-lose-work edit engine: branch+commit+PR for git, backup+diff for non-git; original never changed without approval. | — (approval-gated by design). |

## Verticals (each `async def run_once(...)`)

| Vertical | Tier | What it does |
|----------|------|--------------|
| `github_autopilot` | R → A | Perceive issues/CI (R) → fix in an isolated worktree (act, sandboxed, no creds) → **propose** PR through the broker (A). Test-gated + self-reviewed. |
| `inbox_triage` | R → A | Read Gmail/GChat (R, untrusted) → draft replies in a **credential-free** read session → broker creates the draft (A). |
| `meeting_intel` | R → A | Read calendar/context (R) → prep brief → optional broker-gated actions (A). |
| `reporter` | R | Formats a digest from existing state (work log, action queue, learning stats). Pure read/report. |
| `research_agent` | **R** | Multi-source web research → cited answer. Untrusted web content stays in a credential-free, Bash-free read session (`allowed_tools=[Read, WebSearch, WebFetch]`). No approval needed — it only reports. |

## Why this is safe

Every entry above holds **at most one** leg of the dangerous triad in a single
session:

- Untrusted-input sessions (browsing, research, inbox drafting, issue-fixing) run
  at tier **R** or in a **secret-scrubbed** act sandbox — no live credentials, no
  unconstrained external action.
- The **only** component that combines credentials with external action (push, PR,
  send, event creation) is the **approval broker's executor** — and it never sees
  untrusted input as instructions; it acts on a structured, human-gated payload.

That separation is the whole game: no bridge exists for a prompt-injection payload
in a web page or an email to reach a credential and a mutating action in the same
breath.
