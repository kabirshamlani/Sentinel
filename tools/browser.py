"""Browser Automation -- Playwright-based headless browser for web tasks.

Supports navigating pages, taking screenshots, filling forms, and web research.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from config.paths import SCREENSHOTS_DIR

logger = logging.getLogger("browser-agent")

SCREENSHOT_DIR = SCREENSHOTS_DIR


async def _get_browser():
    """Get a Playwright browser instance."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError("playwright not installed. Run: pip install playwright && playwright install chromium")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    return pw, browser


async def navigate(url: str, screenshot: bool = True) -> dict:
    """Navigate to a URL and optionally take a screenshot."""
    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        title = await page.title()
        content = await page.content()
        text = await page.inner_text("body")

        result: dict[str, Any] = {
            "url": url,
            "title": title,
            "text_preview": text[:3000] if text else "",
        }

        if screenshot:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = SCREENSHOT_DIR / f"nav_{title[:30].replace(' ', '_')}.png"
            await page.screenshot(path=str(path), full_page=False)
            result["screenshot_path"] = str(path)

        return result
    finally:
        await browser.close()
        await pw.stop()


async def take_screenshot(url: str, filename: str = "screenshot.png", full_page: bool = False) -> dict:
    """Take a screenshot of a URL."""
    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SCREENSHOT_DIR / filename
        await page.screenshot(path=str(path), full_page=full_page)

        return {
            "url": url,
            "path": str(path),
            "title": await page.title(),
        }
    finally:
        await browser.close()
        await pw.stop()


async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web using DuckDuckGo and return results."""
    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        search_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

        results = []
        links = await page.query_selector_all(".result__a")

        for link in links[:max_results]:
            title = await link.inner_text()
            href = await link.get_attribute("href")
            parent = await link.evaluate_handle("el => el.closest('.result')")
            snippet_el = await parent.query_selector(".result__snippet")
            snippet = await snippet_el.inner_text() if snippet_el else ""

            results.append({
                "title": title,
                "url": href,
                "snippet": snippet[:300],
            })

        return results
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return [{"error": str(e)}]
    finally:
        await browser.close()
        await pw.stop()


async def fill_form(url: str, fields: dict[str, str], submit_selector: str = "") -> dict:
    """Navigate to a URL, fill form fields, and optionally submit."""
    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        for selector, value in fields.items():
            await page.fill(selector, value)

        if submit_selector:
            await page.click(submit_selector)
            await page.wait_for_load_state("networkidle", timeout=10000)

        return {
            "url": page.url,
            "title": await page.title(),
            "fields_filled": list(fields.keys()),
            "submitted": bool(submit_selector),
        }
    finally:
        await browser.close()
        await pw.stop()


async def get_page_text(url: str) -> dict:
    """Get the full text content of a page (useful for reading articles)."""
    pw, browser = await _get_browser()
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        title = await page.title()
        text = await page.inner_text("body")

        return {
            "url": url,
            "title": title,
            "text": text[:10000],
            "truncated": len(text) > 10000,
        }
    finally:
        await browser.close()
        await pw.stop()
