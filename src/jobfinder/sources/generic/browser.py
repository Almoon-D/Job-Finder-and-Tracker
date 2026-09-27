"""Optional headless-browser fetch for JavaScript-rendered pages (requires the 'browser' extra)."""

from __future__ import annotations

_browser = None
_playwright = None


async def render_page(url: str, wait_for: str | None = None, timeout_ms: int = 45000) -> str:
    global _browser, _playwright
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("fetch: browser needs the 'browser' extra (playwright)") from exc
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch()
    page = await _browser.new_page()
    try:
        await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        if wait_for:
            await page.wait_for_selector(wait_for, timeout=timeout_ms)
        return await page.content()
    finally:
        await page.close()


async def close_browser() -> None:
    global _browser, _playwright
    if _browser is not None:
        await _browser.close()
        _browser = None
    if _playwright is not None:
        await _playwright.stop()
        _playwright = None
