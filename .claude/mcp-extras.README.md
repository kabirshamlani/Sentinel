# Optional MCP servers (opt-in)

These are **not enabled by default**. Each needs extra setup — a network browser, a
database credential, an OAuth sign-in, or a running local process — so they live here
instead of in `.mcp.json`. JSON can't carry comments, so this file documents what each
one is and when to turn it on.

## How to enable one

`mcp-extras.json` uses the same schema as `.mcp.json` (`mcpServers` map, `type`, and
`command`/`args`/`url`). To turn a server on, copy its entry from
`mcp-extras.json` into the `mcpServers` object of `.mcp.json`, complete the setup
below, then restart the agent so it re-reads the config.

Merge only the servers you actually use — every enabled MCP server adds tools and
tokens to the working context, and small high-signal context is what keeps the agent
reliable. Don't enable all four "just in case."

## The servers

### playwright — browser E2E / UI automation
- **Command:** `npx @playwright/mcp@latest`
- **What it does:** drives a real browser so the agent can click through and verify web
  UI end-to-end (not just unit tests).
- **Setup:** needs Node/npx and will download browser binaries on first run
  (`npx playwright install` if it prompts). Enable only when a task touches the
  browser-facing UI.

### postgres-mcp — read-only database access
- **Command:** `uvx postgres-mcp --access-mode=restricted`
- **What it does:** lets the agent inspect schema and run **read-only** queries against a
  Postgres database to ground its reasoning in real data.
- **Setup:** requires the `DATABASE_URI` env var (placeholder in the JSON). Point it at a
  **read-only** role and keep `--access-mode=restricted` so the agent cannot mutate data.
  Never wire it to a production writer credential.

### sentry — error/issue context
- **URL:** `https://mcp.sentry.dev/mcp` (HTTP, OAuth)
- **What it does:** pulls Sentry issues, stack traces, and events so the agent can debug
  against real production errors.
- **Setup:** OAuth sign-in on first connect; you must have a Sentry account/org. Enable
  **only if you actually use Sentry** — otherwise it's dead weight.

### memory — knowledge-graph memory
- **Command:** `npx @modelcontextprotocol/server-memory`
- **What it does:** a persistent knowledge-graph store the agent can read and write
  across sessions.
- **Setup:** none beyond npx, but **situational** — persistent cross-session memory is a
  known memory-poisoning risk (facts written once persist across resets). Enable only for
  workflows that genuinely need durable memory, and review what gets promoted into it.
