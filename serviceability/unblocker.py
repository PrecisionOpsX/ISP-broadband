"""Pluggable unblocker backend for the heavily defended providers (AT&T).

AT&T sits behind Akamai Bot Manager, which free browser automation does not beat
(confirmed: the _abck cookie never validates and the APIs return 403). The
production answer is a commercial Web Unlocker that solves the challenge for us.

Most unlockers expose themselves as an HTTP(S) proxy that returns the unblocked
response (Bright Data Web Unlocker, Oxylabs Web Unblocker). We route the existing
browser checker through that proxy, so nothing else about the AT&T flow changes.
This module is the single seam where that key gets dropped in.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Unblocker:
    """Configuration for a commercial unblocker. Empty means disabled.

    Two shapes are supported. `proxy` is a Web Unlocker proxy string, used for
    single-request targets like AT&T's API. `cdp_endpoint` is a Scraping Browser
    websocket (wss://...), used for the interactive multi-step flows (Frontier),
    where we drive a remote, already-unblocked browser with Playwright.
    """

    provider: str = ""
    proxy: str = ""          # e.g. http://user:pass@host:port (Web Unlocker)
    cdp_endpoint: str = ""   # e.g. wss://...@brd.superproxy.io:9222 (Scraping Browser)
    enabled: bool = False

    def browser_proxy(self) -> str | None:
        """The proxy string to launch the browser through, or None if disabled."""
        return self.proxy if (self.enabled and self.proxy) else None

    def cdp(self) -> str | None:
        """The Scraping Browser websocket to connect to, or None if disabled."""
        return self.cdp_endpoint if (self.enabled and self.cdp_endpoint) else None


def from_config(data: dict | None) -> Unblocker:
    if not data:
        return Unblocker()
    proxy = data.get("proxy", "")
    cdp_endpoint = data.get("cdp_endpoint", "") or data.get("scraping_browser", "")
    return Unblocker(
        provider=data.get("provider", ""),
        proxy=proxy,
        cdp_endpoint=cdp_endpoint,
        enabled=bool(data.get("enabled", False)) and bool(proxy or cdp_endpoint),
    )
