"""Gateway auth — ONE robust resolver for the TrueFoundry gateway token.

WHY THIS EXISTS (RCA 2026-08-14): the army ran brain-dead from Aug 12 21:00 to
Aug 14 because gateway auth was resolved in fragile, inconsistent ways:

  * a static JWT (settings.json / cached env) expired after ~8 days and nothing
    re-resolved it — every LLM call 401'd while logs recorded "success";
  * providers.py read ANTHROPIC_AUTH_TOKEN from ~/.claude/settings.json's env
    block, which no longer holds a static token (auth moved to apiKeyHelper);
  * the apiKeyHelper shell script crashes under launchd (`set -u` + unset $USER)
    and its SDK-refresh path needs a python that has `truefoundry` installed,
    which the bare launchd PATH's python3 does not.

This module is the single source of truth the daemon-side callers use:

    from brain import gateway_auth
    tok  = gateway_auth.gateway_token()   # cached ≤4 min, never a known-expired JWT
    base = gateway_auth.base_url()
    gateway_auth.invalidate()             # call on a 401 so the next call re-resolves

Resolution order (mirrors ~/.truefoundry/claude-token-helper.sh, but pure
Python so it works in ANY environment the daemon runs in):
  1. macOS Keychain item "tfy-gateway-pat" (durable PAT — canonical store)
  2. ~/.truefoundry/api-key.txt holding a durable key (tfy_pat_ / tfy_va_)
  3. JWT from ~/.truefoundry/credentials.json — used only while >10 min from
     expiry; otherwise refreshed IN-PROCESS via the truefoundry SDK (the daemon's
     python has it), falling back to the helper script as a last resort.

Never log or persist the token. Never cache a token past _TTL seconds, and
never serve a JWT that is within 10 minutes of its exp claim.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger("gateway-auth")

_TTL = 240.0                      # short cache: rotation must bite within minutes
_JWT_MIN_REMAINING = 600.0        # treat a JWT with <10 min left as expired

_lock = threading.Lock()
_cache: dict = {"token": None, "at": 0.0}

_TFY_DIR = Path.home() / ".truefoundry"
_KEY_FILE = _TFY_DIR / "api-key.txt"
_CRED_FILE = _TFY_DIR / "credentials.json"
_HELPER = _TFY_DIR / "claude-token-helper.sh"


def base_url() -> str:
    """The gateway base URL Claude Code itself is configured for."""
    try:
        cfg = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        return (cfg.get("env", {}).get("ANTHROPIC_BASE_URL", "") or "").rstrip("/")
    except Exception:
        return ""


def _jwt_seconds_remaining(tok: str) -> float | None:
    """exp-now for a JWT, None if the token isn't a parseable JWT."""
    try:
        if tok.count(".") != 2:
            return None
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims["exp"]) - time.time()
    except Exception:
        return None


def _looks_durable(tok: str) -> bool:
    return tok.startswith(("tfy_pat_", "tfy_va_"))


def _from_keychain() -> str | None:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "tfy-gateway-pat", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        tok = out.stdout.strip()
        if out.returncode == 0 and _looks_durable(tok):
            return tok
    except Exception:
        pass
    return None


def _from_key_file() -> str | None:
    try:
        if _KEY_FILE.exists():
            tok = _KEY_FILE.read_text().strip()
            if _looks_durable(tok):
                return tok
    except OSError:
        pass
    return None


def _from_credentials() -> str | None:
    """JWT from credentials.json; refresh via the SDK when near expiry."""
    try:
        if not _CRED_FILE.exists():
            return None
        tok = (json.loads(_CRED_FILE.read_text()) or {}).get("access_token", "")
        if tok:
            rem = _jwt_seconds_remaining(tok)
            if rem is None or rem > _JWT_MIN_REMAINING:
                return tok
    except Exception:
        pass

    # Near/past expiry — refresh in-process (the daemon's python has the SDK;
    # the helper script's default python3 under launchd does NOT).
    try:
        from truefoundry.common.credential_provider import FileCredentialProvider
        tok = FileCredentialProvider().token.access_token
        if tok:
            logger.info("gateway JWT refreshed via truefoundry SDK")
            return tok
    except Exception as e:
        logger.warning("in-process JWT refresh failed: %s", str(e)[:200])

    # Last resort: the helper script (harden its env so `set -u` can't kill it).
    try:
        if _HELPER.exists():
            env = {**os.environ}
            env.setdefault("USER", os.getenv("LOGNAME", "") or "user")
            out = subprocess.run(["bash", str(_HELPER)], capture_output=True,
                                 text=True, timeout=30, env=env)
            tok = out.stdout.strip()
            if out.returncode == 0 and tok:
                return tok
    except Exception:
        pass
    return None


def gateway_token(force: bool = False) -> str | None:
    """A currently-valid gateway token, or None. Cached for ≤_TTL seconds."""
    with _lock:
        now = time.time()
        if not force and _cache["token"] and now - _cache["at"] < _TTL:
            return _cache["token"]
        tok = _from_keychain() or _from_key_file() or _from_credentials()
        if tok:
            rem = _jwt_seconds_remaining(tok)
            if rem is not None and rem < _JWT_MIN_REMAINING:
                logger.error("resolved gateway JWT is expired/near-expiry — auth is broken")
                tok = None
        _cache.update(token=tok, at=now)
        if not tok:
            logger.error("no valid gateway token could be resolved (keychain/key-file/JWT all failed)")
        return tok


def invalidate() -> None:
    """Drop the cache so the next gateway_token() re-resolves from scratch.
    Call this on any 401 — a rotated key must bite immediately."""
    with _lock:
        _cache.update(token=None, at=0.0)
