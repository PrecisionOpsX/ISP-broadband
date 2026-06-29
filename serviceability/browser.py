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

import random
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

# Pools used to vary the browser identity per request, so a site that rate-limits
# a repeated device fingerprint sees a different one each time.
_USER_AGENTS = [
    DEFAULT_UA,
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
]
_VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080), (1280, 720)]
_TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"]


def random_fingerprint() -> dict:
    """A randomized browser identity (user agent, viewport, timezone)."""
    width, height = random.choice(_VIEWPORTS)
    return {
        "user_agent": random.choice(_USER_AGENTS),
        "viewport": {"width": width, "height": height},
        "timezone_id": random.choice(_TIMEZONES),
        "locale": "en-US",
    }

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


def proxy_with_session(proxy: str, session_id: str) -> str:
    """Pin one exit IP to this request by adding a session id to the username of
    a Bright Data style residential endpoint. A new session id yields a new IP,
    so the IP is stable within one check but fresh for the next one. Non Bright
    Data proxies are returned unchanged.
    """
    from urllib.parse import urlparse
    parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
    user = parsed.username or ""
    if "zone-" not in user:
        return proxy
    if "-country-" not in user:
        user = f"{user}-country-us"
    user = f"{user}-session-{session_id}"
    return f"{parsed.scheme}://{user}:{parsed.password or ''}@{parsed.hostname}:{parsed.port}"


def _proxy_config(proxy: str) -> dict:
    """Turn a proxy URL into Playwright's proxy dict.

    Residential proxies are almost always authenticated, and Playwright wants the
    credentials split out from the server, not left embedded in the URL.
    """
    from urllib.parse import urlparse
    parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
        config["password"] = parsed.password or ""
    return config


def require_playwright() -> None:
    if sync_playwright is None:
        raise RuntimeError(
            "Playwright is required for the AT&T and Kinetic browser checkers. "
            "Install it with: pip install playwright && python -m playwright install chromium"
        )


def launch_session(headless: bool = True, proxy: str | None = None,
                   user_agent: str = DEFAULT_UA,
                   ignore_https_errors: bool = False,
                   fingerprint: dict | None = None,
                   cdp_endpoint: str | None = None) -> BrowserSession:
    require_playwright()
    pw = sync_playwright().start()

    # A Scraping Browser (Bright Data and similar) is a remote, already-unblocked
    # browser we drive over CDP. It manages IP rotation and the anti-bot bypass,
    # so we just connect and use it. Slower than local, hence the larger timeout.
    if cdp_endpoint:
        browser = pw.chromium.connect_over_cdp(cdp_endpoint, timeout=120000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(90000)
        return BrowserSession(playwright=pw, browser=browser, context=context, page=page)

    base_args = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        base_args["proxy"] = _proxy_config(proxy)
    # The installed real Chrome launches reliably here and is harder to detect
    # than the bundled Chromium build. Fall back to Chromium if Chrome is absent.
    try:
        browser = pw.chromium.launch(channel="chrome", **base_args)
    except Exception:
        browser = pw.chromium.launch(**base_args)
    fp = fingerprint or {}
    context = browser.new_context(
        user_agent=fp.get("user_agent", user_agent),
        viewport=fp.get("viewport", {"width": 1366, "height": 768}),
        locale=fp.get("locale", "en-US"),
        timezone_id=fp.get("timezone_id", "America/New_York"),
        ignore_https_errors=ignore_https_errors,
    )
    context.add_init_script(EVASION_SCRIPT)
    page = context.new_page()
    page.set_default_timeout(20000)  # cap element waits so a failure cannot hang
    return BrowserSession(playwright=pw, browser=browser, context=context, page=page)


def launch_persistent_session(user_data_dir: str, headless: bool = True,
                              proxy: str | None = None,
                              user_agent: str = DEFAULT_UA) -> BrowserSession:
    """Like launch_session but with an on-disk profile that keeps cookies across
    runs. Some sites route a cold, cookieless session (an incognito-like context)
    to a dead end; a persistent, warmed profile reads as a returning visitor.
    """
    require_playwright()
    pw = sync_playwright().start()
    args = {
        "headless": headless,
        "user_agent": user_agent,
        "viewport": {"width": 1366, "height": 768},
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if proxy:
        args["proxy"] = _proxy_config(proxy)
    try:
        context = pw.chromium.launch_persistent_context(user_data_dir, channel="chrome", **args)
    except Exception:
        context = pw.chromium.launch_persistent_context(user_data_dir, **args)
    context.add_init_script(EVASION_SCRIPT)
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(20000)
    return BrowserSession(playwright=pw, browser=None, context=context, page=page)
