"""Skill vetter — static, read-only security analysis of agent skills BEFORE
they are ever allowed to run.

Threat model: community/marketplace agent skills are untrusted code. A large
fraction have been found to be actively malicious (data exfiltration, secret
theft, prompt-injection of the host agent, destructive filesystem actions).
Sentinel therefore treats discovery as *read-only*: a skill is scanned as inert
text, scored, and — only if it is not judged malicious — surfaced to a human via
the ApprovalBroker as a CRITICAL, IRREVERSIBLE proposal. Nothing here installs,
executes, imports, or otherwise activates a skill. Malicious skills are refused
outright and never proposed.

Scoring is additive over signature categories; a category contributes its weight
at most once per file so a single noisy file cannot dominate. The regexes are
deliberately anchored to *actions* (a shell/exec form, an actual read command)
rather than mere mentions, so that a skill which legitimately says "read the
file" or names an API key variable is not flagged.

Pure stdlib only (re, json, pathlib, logging).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("skill-vetter")

# --- verdict thresholds ---
MALICIOUS_THRESHOLD = 60
SUSPICIOUS_THRESHOLD = 25

# --- category weights (spec) ---
W_EXFIL = 30
W_SECRET = 40
W_OBFUSCATION = 25
W_INJECTION = 35
W_DANGEROUS_FS = 20

# Files worth scanning as text. We treat *everything* textual as suspect, not
# just SKILL.md, because the dangerous payload is usually in a helper script.
_TEXT_SUFFIXES = {
    ".md", ".txt", ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".rb", ".pl", ".php", ".ps1", ".bat", ".cmd", ".yaml", ".yml", ".json",
    ".toml", ".cfg", ".ini", ".env", ".conf", "",  # extensionless (e.g. a bare script)
}
_MAX_FILE_BYTES = 512 * 1024  # skip anything larger; not a text skill file
_SKIP_DIRS = {".venv", "venv", "node_modules", ".git", "__pycache__", "dist", "build"}

# ---------------------------------------------------------------------------
# Signature regexes (compiled once). Each is anchored to an *action*, not a
# mere mention, to keep false positives off benign skills.
# ---------------------------------------------------------------------------

# Hosts that are legitimate to hit; used to distinguish exfil from normal use.
_ALLOWLISTED_EGRESS = re.compile(
    r"(?:github\.com|githubusercontent\.com|api\.github\.com|"
    r"anthropic\.com|api\.anthropic\.com|openai\.com|api\.openai\.com|"
    r"googleapis\.com|google\.com|pypi\.org|npmjs\.org|npmjs\.com|"
    r"localhost|127\.0\.0\.1)",
    re.IGNORECASE,
)

# --- exfiltration ---
# curl/wget/nc to a raw IPv4 address
_RE_NET_TO_RAW_IP = re.compile(
    r"\b(?:curl|wget|nc|ncat|telnet)\b[^\n|;&]*?\b"
    r"(?:https?://)?(?:\d{1,3}\.){3}\d{1,3}\b",
    re.IGNORECASE,
)
# curl ... | bash  (pipe a download straight into a shell)
_RE_CURL_PIPE_SHELL = re.compile(
    r"\b(?:curl|wget)\b[^\n]*?\|\s*(?:sudo\s+)?(?:ba)?sh\b",
    re.IGNORECASE,
)
# POST / send to a known drop host (pastebin/webhook/discord/telegram/etc.)
_RE_DROP_HOST = re.compile(
    r"(?:curl|wget|nc|fetch|requests\.(?:post|get)|http\.client|urllib)"
    r"[^\n]*?"
    r"(?:pastebin\.com|hastebin\.com|paste\.ee|transfer\.sh|file\.io|"
    r"0x0\.st|ngrok\.io|webhook\.site|requestbin|"
    r"discord(?:app)?\.com/api/webhooks|discordapp\.com/api|"
    r"api\.telegram\.org|hooks\.slack\.com|"
    r"[a-z0-9.-]*\.workers\.dev)",
    re.IGNORECASE,
)
# a bare "curl/wget http(s)://host" where host is NOT allowlisted and not an IP
_RE_NET_TO_HOST = re.compile(
    r"\b(?:curl|wget|nc|ncat)\b[^\n|;&]*?\bhttps?://([a-z0-9][a-z0-9.-]+\.[a-z]{2,})",
    re.IGNORECASE,
)

# --- secret access ---
_RE_SSH_READ = re.compile(
    r"(?:cat|less|more|head|tail|cp|scp|xxd|base64|tar|zip|read|open\()"
    r"[^\n]*~?/\.ssh(?:/|\b)",
    re.IGNORECASE,
)
_RE_AWS_CREDS = re.compile(r"~?/\.aws/credentials", re.IGNORECASE)
_RE_KEYCHAIN_READ = re.compile(
    r"\bsecurity\s+find-(?:generic|internet)-password\b", re.IGNORECASE,
)
# env DUMPS of secret-looking vars. A dump enumerates/exports the whole
# environment (env / printenv / set, piped or redirected) or echoes a secret var
# — NOT reading a single known variable via os.environ.get("X_KEY"), which is
# ordinary config and must not be flagged.
_RE_ENV_DUMP = re.compile(
    r"\bprintenv\b(?!\s+[A-Za-z])"          # bare `printenv` (no single-var arg)
    r"|\benv\b\s*(?:\||>|>>)"               # `env | ...` / `env > file`
    r"|\bset\b\s*(?:\||>)\s*"               # `set | ...` (shell env dump)
    r"|\becho\s+\$\{?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
    re.IGNORECASE,
)
_RE_ENV_GREP_SECRET = re.compile(
    r"\b(?:env|printenv|set)\b[^\n]*?\|[^\n]*?\bgrep\b[^\n]*?"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)",
    re.IGNORECASE,
)
# cat/read of a .env file (an actual read command, not a mere mention)
_RE_CAT_DOTENV = re.compile(
    r"\b(?:cat|less|more|head|tail|source|\.|xxd|base64|cp|scp)\s+[^\n;|&]*\.env\b",
    re.IGNORECASE,
)

# --- obfuscation ---
_RE_B64_DECODE_EXEC = re.compile(
    r"base64\s+(?:--?d(?:ecode)?|-D)\b[^\n]*?\|\s*(?:ba)?sh\b"
    r"|b64decode\([^\n]*\)\s*\)?\s*(?:#[^\n]*)?\n?\s*(?:exec|eval)"
    r"|(?:exec|eval)\s*\(\s*(?:base64\.)?b64decode",
    re.IGNORECASE,
)
_RE_EVAL_EXEC = re.compile(
    r"\b(?:eval|exec)\s*\(|(?<![A-Za-z_.])Function\s*\(\s*['\"]",
)
_RE_HEX_STRING = re.compile(r"(?:\\x[0-9a-fA-F]{2}){20,}")  # >40 hex chars => 20+ \xNN

# --- prompt injection in SKILL.md / docs ---
_RE_INJECTION = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"
    r"|disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules)"
    r"|do\s+not\s+tell\s+the\s+user"
    r"|don'?t\s+tell\s+the\s+user"
    r"|without\s+(?:asking|telling|informing)\s+(?:the\s+)?(?:user|human)"
    r"|without\s+the\s+user'?s?\s+(?:knowledge|consent|approval)"
    r"|disable\s+(?:the\s+)?approval"
    r"|skip\s+(?:the\s+)?(?:permission|approval|confirmation)"
    r"|bypass\s+(?:the\s+)?(?:approval|permission|safety)"
    r"|do\s+not\s+ask\s+(?:for\s+)?(?:permission|approval|confirmation)",
    re.IGNORECASE,
)

# --- dangerous filesystem / privilege ---
_RE_RM_RF = re.compile(r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s", re.IGNORECASE)
_RE_CHMOD_777 = re.compile(r"\bchmod\s+(?:-R\s+)?0?777\b")
_RE_SUDO = re.compile(r"(?<![A-Za-z_-])sudo\s+", re.IGNORECASE)
# writes outside the skill dir: redirect / write to an absolute or ~ path that
# escapes the skill directory (heuristic: absolute paths not under /tmp).
_RE_WRITE_OUTSIDE = re.compile(
    r"(?:>|>>|tee)\s+(?:~|/(?:etc|usr|bin|sbin|Library|System|var/(?!folders)|"
    r"Users/[^/\n]+/\.(?:ssh|aws|config|bashrc|zshrc|profile)))",
    re.IGNORECASE,
)


def _iter_text_files(root: Path):
    """Yield text-ish files under a skill directory (or the file itself)."""
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def _minified_or_oneline(text: str, name: str) -> bool:
    """A one-line file >500 chars, or a code file whose longest line is very
    long, is a minification/obfuscation smell. Skip Markdown/JSON prose."""
    if name.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml", ".toml")):
        return False
    lines = text.splitlines() or [text]
    if len(lines) == 1 and len(text) > 500:
        return True
    longest = max((len(ln) for ln in lines), default=0)
    return longest > 500 and len(lines) < max(1, len(text) // 400)


def scan(skill_path: str) -> dict:
    """Statically analyse a skill directory (or a single SKILL.md/script) and
    return a risk report::

        {"risk_score": int 0-100, "flags": [str, ...],
         "verdict": "safe"|"suspicious"|"malicious"}

    Read-only: opens files as text, never executes or imports anything.
    """
    root = Path(skill_path).expanduser()
    flags: list[str] = []
    score = 0

    if not root.exists():
        return {"risk_score": 0, "flags": [f"path-not-found:{skill_path}"],
                "verdict": "safe", "files_scanned": 0}

    files = list(_iter_text_files(root))
    scanned = 0

    # Each category contributes its weight AT MOST ONCE across the whole scan.
    # The risk is "does this skill do X at all", not "in how many files" — this
    # keeps a legitimate multi-file pattern (e.g. two helpers that both read one
    # API key from the Keychain) from compounding into a false 'malicious'.
    fired_categories: set[str] = set()

    def hit(category: str, weight: int, label: str, rel: str):
        nonlocal score
        flags.append(f"{label} [{rel}]")
        if category not in fired_categories:
            score += weight
            fired_categories.add(category)

    for f in files:
        text = _read_text(f)
        if not text:
            continue
        scanned += 1
        try:
            rel = str(f.relative_to(root)) if root.is_dir() else f.name
        except ValueError:
            rel = f.name

        # ---- exfiltration ----
        if _RE_CURL_PIPE_SHELL.search(text):
            hit("exfil", W_EXFIL, "exfil:curl-pipe-to-shell", rel)
        if _RE_NET_TO_RAW_IP.search(text):
            hit("exfil", W_EXFIL, "exfil:network-to-raw-ip", rel)
        if _RE_DROP_HOST.search(text):
            hit("exfil", W_EXFIL, "exfil:post-to-drop-host(pastebin/webhook/discord/telegram)", rel)
        for m in _RE_NET_TO_HOST.finditer(text):
            host = m.group(1)
            if not _ALLOWLISTED_EGRESS.search(host):
                hit("exfil", W_EXFIL, f"exfil:network-to-non-allowlisted-host({host})", rel)
                break

        # ---- secret access ----
        if _RE_SSH_READ.search(text):
            hit("secret", W_SECRET, "secret:read-of-~/.ssh", rel)
        if _RE_AWS_CREDS.search(text):
            hit("secret", W_SECRET, "secret:read-of-~/.aws/credentials", rel)
        if _RE_KEYCHAIN_READ.search(text):
            hit("secret", W_SECRET, "secret:security-find-generic-password", rel)
        if _RE_ENV_GREP_SECRET.search(text) or _RE_ENV_DUMP.search(text):
            hit("secret", W_SECRET, "secret:env-dump-of-*_KEY/*_TOKEN/*_SECRET", rel)
        if _RE_CAT_DOTENV.search(text):
            hit("secret", W_SECRET, "secret:cat-of-.env", rel)

        # ---- obfuscation ----
        if _RE_B64_DECODE_EXEC.search(text):
            hit("obfuscation", W_OBFUSCATION, "obfuscation:base64-decode-then-exec", rel)
        if _RE_EVAL_EXEC.search(text):
            hit("obfuscation", W_OBFUSCATION, "obfuscation:eval/exec/Function-call", rel)
        if _RE_HEX_STRING.search(text):
            hit("obfuscation", W_OBFUSCATION, "obfuscation:long-hex-encoded-string", rel)
        if _minified_or_oneline(text, f.name):
            hit("obfuscation", W_OBFUSCATION, "obfuscation:minified/one-line-file>500chars", rel)

        # ---- prompt injection (docs/instructions) ----
        if _RE_INJECTION.search(text):
            hit("injection", W_INJECTION, "prompt-injection:instruction-override/secrecy/approval-bypass", rel)

        # ---- dangerous filesystem / privilege ----
        if _RE_RM_RF.search(text):
            hit("dangerous_fs", W_DANGEROUS_FS, "dangerous-fs:rm-rf", rel)
        if _RE_CHMOD_777.search(text):
            hit("dangerous_fs", W_DANGEROUS_FS, "dangerous-fs:chmod-777", rel)
        if _RE_SUDO.search(text):
            hit("dangerous_fs", W_DANGEROUS_FS, "dangerous-fs:sudo", rel)
        if _RE_WRITE_OUTSIDE.search(text):
            hit("dangerous_fs", W_DANGEROUS_FS, "dangerous-fs:write-outside-skill-dir", rel)

    score = min(score, 100)
    if score >= MALICIOUS_THRESHOLD:
        verdict = "malicious"
    elif score >= SUSPICIOUS_THRESHOLD:
        verdict = "suspicious"
    else:
        verdict = "safe"

    return {
        "risk_score": score,
        "flags": flags,
        "verdict": verdict,
        "files_scanned": scanned,
    }


def _summarize(report: dict, name: str) -> str:
    n = len(report.get("flags", []))
    top = "; ".join(report.get("flags", [])[:5])
    return (f"Skill '{name}' scanned {report.get('files_scanned', 0)} file(s): "
            f"risk={report['risk_score']} verdict={report['verdict']} "
            f"({n} flag(s)){': ' + top if top else ''}")


def _run_propose(broker, **kwargs):
    """Call broker.propose whether it is sync or async. If it returns a
    coroutine and no loop is running, drive it to completion; if a loop is
    already running, hand back the coroutine for the caller to await."""
    result = broker.propose(**kwargs)
    if not _is_coroutine(result):
        return result
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)
    # A loop is running: caller is async and must await the returned coroutine.
    return result


def _is_coroutine(obj) -> bool:
    import inspect
    return inspect.iscoroutine(obj) or inspect.isawaitable(obj)


def vet_and_propose(broker, skill_path: str, name: str) -> dict:
    """Scan a skill, then either REFUSE (malicious) or propose an approval-gated,
    CRITICAL + IRREVERSIBLE install via the broker. Never auto-installs.

    Returns the scan report augmented with a ``decision`` field:
      - ``refused``   : malicious; nothing was proposed.
      - ``proposed``  : a proposal was created (see ``proposal``).
    If ``broker.propose`` is async and an event loop is already running, the
    returned ``proposal`` is the un-awaited coroutine (await it in the caller).
    """
    report = scan(skill_path)
    summary = _summarize(report, name)

    if report["verdict"] == "malicious":
        logger.warning("REFUSING malicious skill install: %s | flags=%s",
                       summary, report["flags"])
        return {**report, "decision": "refused", "proposal": None,
                "summary": summary}

    logger.info("Skill vetted (%s); proposing approval-gated install: %s",
                report["verdict"], summary)
    proposal = _run_propose(
        broker,
        action_type="add_skill",
        description=f"Install skill {name}",
        details={"skill_path": skill_path, "name": name, "scan": report,
                 "install_target": ".claude/skills/ (repo-local; NEVER global ~/.claude)"},
        reasoning=summary,
        project="sentinel",
        criticality_hint="critical",
        reversibility="irreversible",
    )
    return {**report, "decision": "proposed", "proposal": proposal,
            "summary": summary}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(scan(tgt), indent=2))
