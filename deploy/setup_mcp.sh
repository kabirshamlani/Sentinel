#!/bin/bash
# Warm up / verify the coding MCP servers so the agent's first run isn't slow and
# any missing prerequisite is surfaced now instead of mid-task.
set -uo pipefail
SENTINEL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Sentinel MCP setup =="
ok=0; warn=0

need() { command -v "$1" >/dev/null 2>&1 && echo "  ✓ $1" || { echo "  ✗ $1 MISSING ($2)"; warn=$((warn+1)); }; }
echo "Prereqs:"
need uvx "install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
need npx "install Node (nvm)"

echo "Warming Serena (uvx build + LSP deps, first time only)…"
if timeout 180 uvx --from git+https://github.com/oraios/serena serena --help >/dev/null 2>&1; then
  echo "  ✓ serena reachable via uvx"; ok=$((ok+1))
else
  echo "  ⚠ serena warm-up timed out — it will build on first real use"; warn=$((warn+1))
fi

echo "Warming fetch MCP…"
timeout 60 uvx mcp-server-fetch --help >/dev/null 2>&1 && { echo "  ✓ mcp-server-fetch reachable"; ok=$((ok+1)); } || echo "  ⚠ mcp-server-fetch will fetch on first use"

echo "Validating .mcp.json…"
python3 -c "import json,sys; json.load(open('$SENTINEL_DIR/.mcp.json')); print('  ✓ .mcp.json valid')" || warn=$((warn+1))

echo
echo "context7 + github MCP are HTTP (no local install); github MCP prompts for OAuth"
echo "on first use (or set GITHUB_PERSONAL_ACCESS_TOKEN). Optional servers: see"
echo ".claude/mcp-extras.README.md"
echo
echo "Done. ok=$ok warn=$warn"
