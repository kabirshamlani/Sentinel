"""Web Tools -- a safe, defensive web capability layer for agents.

Complements tools/browser.py (the full Playwright wrapper) with lighter-weight,
crash-proof primitives that a Python caller (a vertical, the reporter, a health
check) can use directly without spinning up Claude Code:

  * web_search(query)  -> [{title, url, snippet}]  (DuckDuckGo HTML)
  * read_url(url)      -> {url, title, text (readable, ~15k), links}
  * screenshot(url)    -> {url, path}               (via tools/browser.py)

SAFETY / Rule-of-Two: everything a page returns is UNTRUSTED input. These
functions only *read* — they never carry credentials and never take an external
mutating action, so pulling web content into the process is safe. Verticals that
hand this untrusted text to an LLM must do so at tier="read" (no Bash/Write/creds),
which is exactly what verticals/research_agent.py does.

Defensive by construction: every optional dependency is import-guarded, every
network call is timeout-bounded, and every entry point returns a structured dict
(or a list) with an "error" key rather than raising — a bad URL must never crash
the caller's cycle.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import unescape
from urllib.parse import quote_plus, urljoin, urlparse

logger = logging.getLogger("web-tools")

# Generous but bounded: readable body text is truncated so a huge page can't blow
# up a downstream LLM context or the process memory.
MAX_TEXT_CHARS = 15_000
MAX_LINKS = 40
DEFAULT_TIMEOUT = 20.0

# A plain, honest UA. Some sites 403 the default httpx UA; this is not cloaking.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Sentinel/1.0"
)

# Tags whose text is never useful readable content.
_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "head",
               "nav", "footer", "form", "iframe")


# ----------------------------------------------------------------------------- #
# internal helpers
# ----------------------------------------------------------------------------- #

async def _fetch(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch a URL with httpx. Returns {url, status, html} or {error}.

    Import-guarded: if httpx is missing we say so rather than raising.
    """
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed. Run: pip install httpx"}

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            resp = await client.get(url)
            return {
                "url": str(resp.url),
                "status": resp.status_code,
                "html": resp.text,
            }
    except Exception as e:  # network error, DNS, TLS, timeout, ...
        logger.warning("web_tools fetch failed for %s: %s", url, e)
        return {"error": f"fetch failed: {e}", "url": url}


def _readable_text(html: str) -> str:
    """Strip HTML down to readable body text.

    Prefers BeautifulSoup for correctness; falls back to a regex strip so the
    function still works when bs4 isn't installed.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except ImportError:
        # regex fallback: drop scripts/styles, strip remaining tags, unescape.
        cleaned = re.sub(r"(?is)<(script|style|noscript|head).*?>.*?</\1>", " ", html)
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        text = unescape(cleaned)

    # collapse runs of whitespace/blank lines
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return unescape(m.group(1).strip()) if m else ""


def _extract_links(html: str, base_url: str) -> list[dict]:
    """Return up to MAX_LINKS absolute {url, text} pairs from the page."""
    links: list[dict] = []
    seen: set[str] = set()
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = a.get("href", "").strip()
            text = a.get_text(strip=True)
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            absolute = urljoin(base_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append({"url": absolute, "text": text[:120]})
            if len(links) >= MAX_LINKS:
                break
    except ImportError:
        for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or ""):
            href = m.group(1).strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            absolute = urljoin(base_url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            text = unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2))).strip()
            links.append({"url": absolute, "text": text[:120]})
            if len(links) >= MAX_LINKS:
                break
    return links


def _clean_ddg_href(href: str) -> str:
    """DuckDuckGo HTML wraps result links in a redirect (//duckduckgo.com/l/?uddg=...)."""
    if not href:
        return ""
    try:
        parsed = urlparse(href if "://" in href else "https:" + href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            from urllib.parse import parse_qs, unquote

            qs = parse_qs(parsed.query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        return href if "://" in href else "https:" + href
    except Exception:
        return href


# ----------------------------------------------------------------------------- #
# public API
# ----------------------------------------------------------------------------- #

async def web_search(query: str, max_results: int = 8) -> list[dict]:
    """Search the web and return [{title, url, snippet}].

    Implementation: DuckDuckGo's HTML endpoint (no API key required, no JS). This
    is the same untrusted-input source as any browsing — callers must treat the
    snippets/URLs as UNTRUSTED. When a dedicated WebSearch tool is available (e.g.
    a Claude Code agent run at tier="read"), prefer that; this is the credential-
    free Python fallback for the fetch-based path.

    Never raises: on any failure returns [{"error": "..."}].
    """
    if not query or not query.strip():
        return [{"error": "empty query"}]

    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    fetched = await _fetch(url)
    if "error" in fetched:
        return [{"error": fetched["error"]}]

    html = fetched.get("html", "")
    results: list[dict] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for res in soup.select(".result")[: max_results * 2]:
            a = res.select_one(".result__a")
            if not a:
                continue
            href = _clean_ddg_href(a.get("href", ""))
            if not href:
                continue
            snip_el = res.select_one(".result__snippet")
            results.append({
                "title": a.get_text(strip=True),
                "url": href,
                "snippet": (snip_el.get_text(strip=True) if snip_el else "")[:300],
            })
            if len(results) >= max_results:
                break
    except ImportError:
        # regex fallback over the DDG HTML result anchors
        for m in re.finditer(
            r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html
        ):
            href = _clean_ddg_href(m.group(1))
            if not href:
                continue
            title = unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2))).strip()
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= max_results:
                break

    if not results:
        return [{"error": "no results (search may be rate-limited or blocked)"}]
    return results


async def read_url(url: str) -> dict:
    """Fetch a page and return {url, title, text (readable, ~15k), links}.

    Uses httpx + a readability strip (bs4 when present, regex fallback otherwise).
    All returned text is UNTRUSTED web content — never feed it to a credentialed
    or act-tier session.

    Never raises: on any failure returns {"error": "...", "url": url}.
    """
    if not url or not url.strip():
        return {"error": "empty url", "url": url}
    if not urlparse(url).scheme:
        url = "https://" + url

    fetched = await _fetch(url)
    if "error" in fetched:
        return {"error": fetched["error"], "url": url}

    html = fetched.get("html", "")
    final_url = fetched.get("url", url)
    text = _readable_text(html)
    truncated = len(text) > MAX_TEXT_CHARS
    return {
        "url": final_url,
        "status": fetched.get("status"),
        "title": _extract_title(html),
        "text": text[:MAX_TEXT_CHARS],
        "truncated": truncated,
        "links": _extract_links(html, final_url),
    }


async def screenshot(url: str, path: str | None = None) -> dict:
    """Screenshot a URL via the Playwright wrapper in tools/browser.py.

    Saves under SCREENSHOTS_DIR (config.paths). Returns {url, path, title} or
    {"error": ...}. Import- and exception-guarded: if Playwright isn't installed
    the caller gets a structured error, not a crash.
    """
    if not url or not url.strip():
        return {"error": "empty url"}
    if not urlparse(url).scheme:
        url = "https://" + url

    try:
        from tools import browser
    except Exception as e:  # pragma: no cover - import-time only
        return {"error": f"browser module unavailable: {e}", "url": url}

    # Derive a safe filename under SCREENSHOTS_DIR when none is given.
    if path:
        from pathlib import Path

        filename = Path(path).name
    else:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(url).netloc or "page")[:40]
        filename = f"web_{slug or 'page'}.png"

    try:
        # browser.take_screenshot already writes into SCREENSHOTS_DIR.
        result = await browser.take_screenshot(url, filename=filename)
        return result
    except ImportError as e:
        return {"error": str(e), "url": url}
    except Exception as e:
        logger.warning("screenshot failed for %s: %s", url, e)
        return {"error": f"screenshot failed: {e}", "url": url}


async def read_many(urls: list[str], concurrency: int = 4) -> list[dict]:
    """Convenience: read several URLs concurrently (bounded). Never raises.

    Used by research verticals to pull the top few hits of a search in parallel.
    """
    if not urls:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: str) -> dict:
        async with sem:
            return await read_url(u)

    return await asyncio.gather(*(_one(u) for u in urls))
