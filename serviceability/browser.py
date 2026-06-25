"""Warmed browser sessions for sites that fingerprint clients.

AT&T sits behind Akamai Bot Manager, which checks the TLS fingerprint, header
order, and whether the client ran its sensor JavaScript and carries the cookies
it sets. A raw HTTP client fails all three. A real browser passes them for free.
We load the site, let its bot JS run, and then make our lookup calls from inside
that warmed page so they inherit its cookies and fingerprint.

Playwright is an optional import so the rest of the system (models, storage,
comparison, CSV) runs and tests without a browser installed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Prefer patchright (a stealth-patched Playwright) since it defeats the CDP
# fingerprinting that bot walls key on. Fall back to stock Playwright if it is
# not installed. Both expose the same sync API.
try:
    from patchright.sync_api import sync_playwright
    STEALTH = True
except ImportError:
    try:
        from playwright.sync_api import sync_playwright
        STEALTH = False
    except ImportError:
        sync_playwright = None
        STEALTH = False


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Light evasions for the most obvious headless tells. For production hardening,
# layer playwright-stealth on top of this; the hook is install_evasions().
EVASION_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


@dataclass
class BrowserSession:
    """A live browser context plus its page. One session per proxy identity."""

    playwright: object
    browser: object
    context: object
    page: object

    def close(self) -> None:
        for resource in (self.context, self.browser):
            try:
                resource.close()
            except Exception:
                pass
        try:
            self.playwright.stop()
        except Exception:
            pass


def require_playwright() -> None:
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is required for the AT&T and Kinetic browser checkers. "
            "Install it with: pip install playwright && python -m playwright install chromium"
        )


def launch_session(headless: bool = True, proxy: str | None = None,
                   user_agent: str = DEFAULT_UA,
                   ignore_https_errors: bool = False) -> BrowserSession:
    require_playwright()
    pw = sync_playwright().start()
    base_args = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        base_args["proxy"] = {"server": proxy}
    # The installed real Chrome launches reliably here and is harder to detect
    # than the bundled Chromium build. Fall back to Chromium if Chrome is absent.
    try:
        browser = pw.chromium.launch(channel="chrome", **base_args)
    except Exception:
        browser = pw.chromium.launch(**base_args)
    context = browser.new_context(
        user_agent=user_agent,
        viewport={"width": 1366, "height": 768},
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=ignore_https_errors,
    )
    context.add_init_script(EVASION_SCRIPT)
    page = context.new_page()
    return BrowserSession(playwright=pw, browser=browser, context=context, page=page)
