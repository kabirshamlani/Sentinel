"""MCP registry — vet and (approval-gate) the installation of MCP servers.

Same security posture as the skill vetter: an MCP server is untrusted until a
human approves it. `vet_mcp` scores a server manifest against a small set of
signatures; `vet_and_propose` refuses anything judged malicious and otherwise
surfaces a CRITICAL + IRREVERSIBLE proposal via the ApprovalBroker. Nothing here
writes MCP config — the install target is the repo-local `.claude/` only and is
recorded in the proposal for the human to review. Global config (`~/.claude`) is
NEVER touched.

`discover` is a stub: it returns a hardcoded shortlist of well-known-safe MCP
servers. Real discovery (web search / a research agent / a registry
crawl) is intended to be wired in later; see the function docstring.

Pure stdlib only (re, json, logging).
"""

from __future__ import annotations

import inspect
import json
import logging
import re

logger = logging.getLogger("mcp-registry")

# --- verdict thresholds (identical to skill_vetter) ---
MALICIOUS_THRESHOLD = 60
SUSPICIOUS_THRESHOLD = 25

# --- signature weights (spec) ---
W_UNPINNED = 20        # version == "latest" or missing
W_UNKNOWN_PUBLISHER = 15
W_SCOPE_ESCALATION = 25  # write/delete/exec scopes when stated purpose is read
W_UNDECLARED_EGRESS = 20  # network egress to hosts not in the declared set

# Small allowlist of trusted MCP publishers. Extend deliberately, not casually.
ALLOWLIST = {
    "anthropic",
    "modelcontextprotocol",
    "github",
    "google",
    "supabase",
    "awslabs",
}

# tool-scope tokens that mutate/execute (dangerous when the purpose is "read").
# Delimited by anything that is NOT a lowercase letter, so it matches these
# tokens inside snake_case / kebab / camel tool names too, e.g. `delete_file`,
# `exec-shell`, `writeFile`.
_MUTATING_SCOPE = re.compile(
    r"(?<![a-z])(write|create|update|delete|remove|drop|exec|execute|run|shell|"
    r"command|mutate|modify|put|post|push|admin|sudo)(?![a-z])",
    re.IGNORECASE,
)
_READ_PURPOSE = re.compile(
    r"(?<![a-z])(read|readonly|fetch|get|query|search|browse|view|inspect|"
    r"lookup|retrieve|analy[sz]e|report|list)(?![a-z])",
    re.IGNORECASE,
)

# raw-IP or clearly-suspicious egress targets
_RE_RAW_IP = re.compile(r"^(?:https?://)?(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", re.IGNORECASE)
_RE_HOST = re.compile(r"https?://([a-z0-9][a-z0-9.-]+\.[a-z]{2,})", re.IGNORECASE)


def _host_of(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    m = _RE_HOST.search(value)
    if m:
        return m.group(1).lower()
    # raw IP (with or without scheme) -> return the IP itself
    ip = _RE_RAW_IP.match(value)
    if ip:
        return re.sub(r"^https?://", "", value).split("/")[0].split(":")[0].lower()
    # strip any scheme, then take bare host[:port]
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value, flags=re.IGNORECASE)
    return value.split("/")[0].split(":")[0].strip().lower()


def _normalize_hosts(values) -> set[str]:
    hosts = set()
    if isinstance(values, str):
        values = [values]
    for v in values or []:
        h = _host_of(str(v))
        if h:
            hosts.add(h)
    return hosts


def vet_mcp(manifest: dict) -> dict:
    """Score an MCP server manifest and return a risk report::

        {"risk_score": int 0-100, "flags": [str, ...],
         "verdict": "safe"|"suspicious"|"malicious"}

    Expected manifest keys (all optional; missing keys are themselves signals):
        name, command | url, version, declared_tools, publisher,
        purpose | description, declared_hosts | egress
    """
    flags: list[str] = []
    score = 0

    name = str(manifest.get("name", "") or "unknown")
    version = manifest.get("version")
    publisher = str(manifest.get("publisher", "") or "").strip().lower()
    purpose = " ".join(str(manifest.get(k, "")) for k in ("purpose", "description", "summary"))
    tools = manifest.get("declared_tools", manifest.get("tools", [])) or []
    if isinstance(tools, str):
        tools = [tools]

    # (1) version must be pinned
    if version in (None, "", "latest", "*"):
        score += W_UNPINNED
        flags.append(f"unpinned-version(version={version!r}); pin an exact version")

    # (2) publisher must be in the allowlist
    if publisher not in ALLOWLIST:
        score += W_UNKNOWN_PUBLISHER
        flags.append(f"unknown-publisher({publisher or 'missing'}); not in ALLOWLIST")

    # (3) scope escalation: mutating/exec tools while purpose reads read-only
    stated_read = bool(_READ_PURPOSE.search(purpose)) and not _MUTATING_SCOPE.search(purpose)
    mutating = sorted({t for t in map(str, tools) if _MUTATING_SCOPE.search(t)})
    if mutating and stated_read:
        score += W_SCOPE_ESCALATION
        flags.append(
            f"scope-escalation: read-only purpose but mutating/exec tools {mutating}"
        )

    # (4) network egress to non-declared hosts
    declared = _normalize_hosts(manifest.get("declared_hosts", manifest.get("declared_egress", [])))
    # add the server's own command/url host as implicitly declared
    for k in ("url", "endpoint", "command"):
        h = _host_of(str(manifest.get(k, "")))
        if h:
            declared.add(h)
    egress = _normalize_hosts(manifest.get("egress", manifest.get("network_egress", [])))
    undeclared = sorted(egress - declared)
    # also flag any raw-IP egress outright
    raw_ip_egress = [e for e in (manifest.get("egress", []) or [])
                     if isinstance(e, str) and _RE_RAW_IP.match(e.strip())]
    if undeclared or raw_ip_egress:
        score += W_UNDECLARED_EGRESS
        detail = undeclared or [str(x) for x in raw_ip_egress]
        flags.append(f"undeclared-network-egress: {detail}")

    score = min(score, 100)
    if score >= MALICIOUS_THRESHOLD:
        verdict = "malicious"
    elif score >= SUSPICIOUS_THRESHOLD:
        verdict = "suspicious"
    else:
        verdict = "safe"

    return {"name": name, "risk_score": score, "flags": flags, "verdict": verdict}


def discover(query: str) -> list[dict]:
    """STUB discovery of candidate MCP servers.

    Returns a hardcoded shortlist of well-known-safe MCP servers from the
    official `modelcontextprotocol` publisher. This is intentionally NOT real
    discovery: wiring live discovery (web search, a research agent,
    or a signed MCP registry crawl) is a follow-up. Every returned candidate
    still MUST pass `vet_mcp` and human approval before any install — discovery
    is read-only and never installs anything.

    No silent capping: the full hardcoded list is returned as-is.
    """
    logger.info("discover(%r): STUB — returning hardcoded well-known-safe MCP "
                "shortlist; real web/sherlock discovery to be wired later.", query)

    candidates: list[dict] = [
        {
            "name": "filesystem",
            "command": "npx -y @modelcontextprotocol/server-filesystem",
            "version": "0.6.2",
            "publisher": "modelcontextprotocol",
            "purpose": "Read and write files within an allowlisted directory.",
            "declared_tools": ["read_file", "list_directory", "write_file"],
            "declared_hosts": [],
        },
        {
            "name": "github",
            "command": "npx -y @modelcontextprotocol/server-github",
            "version": "0.6.2",
            "publisher": "github",
            "purpose": "Read repositories, issues and pull requests.",
            "declared_tools": ["search_repositories", "get_file_contents", "list_issues"],
            "declared_hosts": ["https://api.github.com"],
        },
        {
            "name": "fetch",
            "command": "npx -y @modelcontextprotocol/server-fetch",
            "version": "0.6.2",
            "publisher": "modelcontextprotocol",
            "purpose": "Fetch and read the contents of a URL for the model.",
            "declared_tools": ["fetch"],
            "declared_hosts": [],
        },
    ]
    logger.info("discover(%r): returning %d candidate(s) (stub, full list, no cap).",
                query, len(candidates))
    return candidates


def create_agent_spec(name: str, role: str, tools: list[str]) -> dict:
    """Build a broker-ready ``create_agent`` proposal payload for a `.claude/
    agents/<slug>.md` subagent. Does NOT write any file — it returns the
    proposed content + a propose(...) kwargs dict for the human to approve.

    Thin wrapper around `discovery.agent_factory.create_agent_spec` so callers
    can build agent proposals from the registry too.
    """
    from discovery import agent_factory
    return agent_factory.create_agent_spec(name, role, tools)


# ---------------------------------------------------------------------------
# approval-gated install
# ---------------------------------------------------------------------------

def _summarize(report: dict) -> str:
    n = len(report.get("flags", []))
    top = "; ".join(report.get("flags", [])[:5])
    return (f"MCP server '{report.get('name', '?')}': "
            f"risk={report['risk_score']} verdict={report['verdict']} "
            f"({n} flag(s)){': ' + top if top else ''}")


def _is_awaitable(obj) -> bool:
    return inspect.iscoroutine(obj) or inspect.isawaitable(obj)


def _run_propose(broker, **kwargs):
    """Call broker.propose whether sync or async; drive to completion if no loop
    is running, else hand the coroutine back for the async caller to await."""
    result = broker.propose(**kwargs)
    if not _is_awaitable(result):
        return result
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    return result


def vet_and_propose(broker, manifest: dict) -> dict:
    """Vet an MCP server manifest, then either REFUSE (malicious) or propose an
    approval-gated, CRITICAL + IRREVERSIBLE install via the broker.

    Install target is the repo-local `.claude/` ONLY (never global ~/.claude);
    that constraint is recorded in the proposal details and reasoning. Never
    auto-installs and never writes config directly.
    """
    report = vet_mcp(manifest)
    summary = _summarize(report)
    name = report.get("name", "unknown")

    if report["verdict"] == "malicious":
        logger.warning("REFUSING malicious MCP server: %s | flags=%s",
                       summary, report["flags"])
        return {**report, "decision": "refused", "proposal": None, "summary": summary}

    logger.info("MCP server vetted (%s); proposing approval-gated install: %s",
                report["verdict"], summary)
    reasoning = (summary +
                 " | Install target: repo-local .claude/ ONLY (never global ~/.claude).")
    proposal = _run_propose(
        broker,
        action_type="add_mcp_server",
        description=f"Add MCP server {name}",
        details={
            "manifest": manifest,
            "vet": report,
            "install_target": ".claude/ (repo-local; NEVER global ~/.claude)",
        },
        reasoning=reasoning,
        project="sentinel",
        criticality_hint="critical",
        reversibility="irreversible",
    )
    return {**report, "decision": "proposed", "proposal": proposal, "summary": summary}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    for cand in discover("filesystem access"):
        print(json.dumps(vet_mcp(cand), indent=2))
