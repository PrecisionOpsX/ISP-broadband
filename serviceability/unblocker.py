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
    """Configuration for a commercial unblocker proxy. Empty means disabled."""

    provider: str = ""
    proxy: str = ""        # e.g. http://user:pass@host:port from the vendor
    enabled: bool = False

    def browser_proxy(self) -> str | None:
        """The proxy string to launch the browser through, or None if disabled."""
        return self.proxy if (self.enabled and self.proxy) else None


def from_config(data: dict | None) -> Unblocker:
    if not data:
        return Unblocker()
    return Unblocker(
        provider=data.get("provider", ""),
        proxy=data.get("proxy", ""),
        enabled=bool(data.get("enabled", False)) and bool(data.get("proxy")),
    )
