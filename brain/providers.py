"""Multi-provider model access — every LLM the owner's keys can reach.

Registry verified 2026-07-13 (research doc 09). Availability is resolved at
RUNTIME from: env -> macOS keychain -> known .env files (The owner explicitly wants
Sentinel to use any key on this machine). Anthropic goes through the same
TrueFoundry gateway Claude Code uses (read from ~/.claude/settings.json), so
there's no separate Anthropic key to manage.

Raw API calls here are for chat/judge/racing — agentic coding still runs
through Claude Code CLI (brain/claude_runner.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import httpx

logger = logging.getLogger("providers")

_ENV_FILES = [Path(p).expanduser() for p in os.environ.get("SENTINEL_ENV_FILES", "").split(":") if p]

# model_id -> (provider, $/Mtok_in, $/Mtok_out, strengths)
REGISTRY: dict[str, dict] = {
    "claude-fable-5":    {"provider": "anthropic", "in": 10.0, "out": 50.0, "good_at": "hardest reasoning, long-horizon"},
    "claude-opus-4-8":   {"provider": "anthropic", "in": 5.0,  "out": 25.0, "good_at": "agentic coding (SOTA)"},
    "claude-sonnet-5":   {"provider": "anthropic", "in": 3.0,  "out": 15.0, "good_at": "workhorse coding/reasoning"},
    "claude-haiku-4-5":  {"provider": "anthropic", "in": 1.0,  "out": 5.0,  "good_at": "speed, classify, judge"},
    "gpt-5.6-sol":       {"provider": "openai",    "in": 5.0,  "out": 30.0, "good_at": "CLI/multi-step coding"},
    "gpt-5.6-terra":     {"provider": "openai",    "in": 2.5,  "out": 15.0, "good_at": "balanced quality/cost"},
    "gpt-5.6-luna":      {"provider": "openai",    "in": 1.0,  "out": 6.0,  "good_at": "fast/cheap"},
    "gemini-3.1-pro":    {"provider": "gemini",    "in": 2.0,  "out": 12.0, "good_at": "2M long-context"},
    "gemini-3.5-flash":  {"provider": "gemini",    "in": 1.5,  "out": 9.0,  "good_at": "best $/quality mid-tier, coding"},
    "grok-4.5":          {"provider": "xai",       "in": 2.0,  "out": 6.0,  "good_at": "frontier-adjacent coding, cheap"},
}

_PROVIDER_KEY = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY", "xai": "XAI_API_KEY"}
_key_cache: dict[str, str | None] = {}


def resolve_key(name: str) -> str | None:
    """env -> keychain -> known .env files. Cached per process."""
    if name in _key_cache:
        return _key_cache[name]
    val = os.getenv(name) or None
    if not val:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", name, "-w"],
                                 capture_output=True, text=True, timeout=5)
            val = out.stdout.strip() or None if out.returncode == 0 else None
        except Exception:
            val = None
    if not val:
        for envf in _ENV_FILES:
            try:
                if envf.exists():
                    for line in envf.read_text().splitlines():
                        if line.strip().startswith(f"{name}="):
                            v = line.split("=", 1)[1].strip().strip('"\'')
                            if v:
                                val = v
                                break
            except OSError:
                continue
            if val:
                break
    _key_cache[name] = val
    return val


def _anthropic_gateway() -> tuple[str, str] | None:
    """(base_url, bearer) for the TrueFoundry gateway.

    RCA 2026-08-14: this used to read a STATIC token from ~/.claude/settings.json's
    env block — which expired Aug 12 (36h of silent 401s) and then disappeared
    entirely when auth moved to apiKeyHelper. Now the token comes from
    brain.gateway_auth (keychain PAT → key file → auto-refreshed JWT, ≤4 min
    cache), so rotation/refresh bites without a daemon restart."""
    try:
        cfg = json.loads((Path.home() / ".claude/settings.json").read_text())
        env = cfg.get("env", {})
        base = env.get("ANTHROPIC_BASE_URL", "")
        tok = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""
        if base and not tok:
            from brain import gateway_auth
            tok = gateway_auth.gateway_token() or ""
        if base and tok:
            return base.rstrip("/"), tok
    except Exception as e:
        logger.warning("gateway resolution failed: %s", str(e)[:200])
    tok = resolve_key("ANTHROPIC_API_KEY")
    if tok:
        return "https://api.anthropic.com", tok
    return None


def available_models() -> list[str]:
    out = []
    for mid, meta in REGISTRY.items():
        p = meta["provider"]
        if p == "anthropic":
            if _anthropic_gateway():
                out.append(mid)
        elif resolve_key(_PROVIDER_KEY[p]):
            out.append(mid)
    return out


def price(model: str, tokens_in: int, tokens_out: int) -> float:
    m = REGISTRY.get(model, {"in": 3.0, "out": 15.0})
    return tokens_in / 1e6 * m["in"] + tokens_out / 1e6 * m["out"]


async def call_model(model: str, prompt: str, system: str = "",
                     max_tokens: int = 1500, timeout: float = 90) -> dict:
    """Uniform async call. Returns {text, model, latency_s, tokens_in, tokens_out,
    cost_usd} or {error}."""
    meta = REGISTRY.get(model)
    if not meta:
        return {"error": f"unknown model {model}", "model": model}
    t0 = time.monotonic()
    try:
        if meta["provider"] == "anthropic":
            gw = _anthropic_gateway()
            if not gw:
                return {"error": "no anthropic gateway", "model": model}
            base, tok = gw
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}]}
            if system:
                body["system"] = system
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{base}/v1/messages", json=body,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "x-api-key": tok,
                                          "anthropic-version": "2023-06-01"})
                # 401 = stale/rotated token. Invalidate the cached one, force a
                # fresh resolve, retry ONCE. (RCA 2026-08-14: without this, an
                # expired token 401'd every call for 36h straight.)
                if r.status_code == 401:
                    from brain import gateway_auth
                    gateway_auth.invalidate()
                    fresh = gateway_auth.gateway_token(force=True)
                    if fresh and fresh != tok:
                        r = await c.post(f"{base}/v1/messages", json=body,
                                         headers={"Authorization": f"Bearer {fresh}",
                                                  "x-api-key": fresh,
                                                  "anthropic-version": "2023-06-01"})
            d = r.json()
            if r.status_code != 200:
                return {"error": str(d)[:300], "model": model}
            text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
            ti = d.get("usage", {}).get("input_tokens", len(prompt) // 4)
            to = d.get("usage", {}).get("output_tokens", len(text) // 4)
        elif meta["provider"] == "gemini":
            key = resolve_key("GEMINI_API_KEY")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={key}")
            body = {"contents": [{"parts": [{"text": (system + "\n\n" if system else "") + prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_tokens}}
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(url, json=body)
            d = r.json()
            if r.status_code != 200:
                return {"error": str(d)[:300], "model": model}
            cands = d.get("candidates", [])
            text = "".join(p.get("text", "") for cd in cands[:1]
                           for p in cd.get("content", {}).get("parts", []))
            um = d.get("usageMetadata", {})
            ti = um.get("promptTokenCount", len(prompt) // 4)
            to = um.get("candidatesTokenCount", len(text) // 4)
        else:  # openai-protocol (openai, xai)
            key = resolve_key(_PROVIDER_KEY[meta["provider"]])
            if not key:
                return {"error": f"no key for {meta['provider']}", "model": model}
            base = "https://api.x.ai" if meta["provider"] == "xai" else "https://api.openai.com"
            body = {"model": model,
                    "messages": ([{"role": "system", "content": system}] if system else []) +
                                [{"role": "user", "content": prompt}]}
            # GPT-5.x wants max_completion_tokens; older + xai accept max_tokens
            if meta["provider"] == "openai" and model.startswith("gpt-5"):
                body["max_completion_tokens"] = max_tokens
            else:
                body["max_tokens"] = max_tokens
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{base}/v1/chat/completions", json=body,
                                 headers={"Authorization": f"Bearer {key}"})
            d = r.json()
            if r.status_code != 200:
                return {"error": str(d)[:300], "model": model}
            text = d.get("choices", [{}])[0].get("message", {}).get("content", "")
            u = d.get("usage", {})
            ti = u.get("prompt_tokens", len(prompt) // 4)
            to = u.get("completion_tokens", len(text) // 4)
        lat = time.monotonic() - t0
        return {"text": text, "model": model, "latency_s": round(lat, 2),
                "tokens_in": ti, "tokens_out": to, "cost_usd": round(price(model, ti, to), 5)}
    except Exception as e:
        return {"error": str(e)[:300], "model": model,
                "latency_s": round(time.monotonic() - t0, 2)}


async def generate_image(prompt: str, out_path: str) -> dict:
    """Image generation via Nano Banana 2 on the Gemini key (cheapest available)."""
    key = resolve_key("GEMINI_API_KEY")
    if not key:
        return {"error": "no gemini key"}
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"gemini-3.1-flash-image:generateContent?key={key}")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]}}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=body)
        d = r.json()
        for cand in d.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    import base64
                    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(out_path).write_bytes(base64.b64decode(part["inlineData"]["data"]))
                    return {"path": out_path}
        return {"error": str(d)[:300]}
    except Exception as e:
        return {"error": str(e)[:300]}
