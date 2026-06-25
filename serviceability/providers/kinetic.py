"""Kinetic (Windstream) serviceability checker.

Kinetic is Windstream's consumer fiber brand, so on the FCC map it appears under
Windstream, not Kinetic. The serviceability flow, confirmed by live inspection,
is: the buy.gokinetic.com shop page takes an address, resolves it through a
Precisely autocomplete, then calls its own qualification API,
/api/v1/address/search, which returns the technology and qualified speed. We
drive that flow and read the JSON, so the verdict is structured, not scraped.

Example real response field set: techType ("FIBER"), maxQual ("QUAL UP TO 2 GIG
RANGE VIA FIBER"), broadbandService.finalQualSpeed (Kbps). Kinetic is lightly
defended compared to AT&T, so a warmed browser session is enough here.
"""

from __future__ import annotations

import json

from ..browser import BrowserSession, launch_session
from ..interface import ProviderChecker
from ..models import AddressInput, CheckResult, ResultCategory
from ..pacing import Blocked, PacingPolicy, with_retries

BUY_URL = "https://buy.gokinetic.com/"
ADDRESS_INPUT_SELECTOR = "#Address, input[name='address']"
SEARCH_API_HINT = "/api/v1/address/search"
CHECK_BUTTON = ("button:has-text('available'), button:has-text('Check'), "
                "button:has-text('Shop'), button[type='submit']")
BLOCK_MARKERS = ("Access Denied", "Request unsuccessful", "Pardon Our Interruption")


class KineticChecker(ProviderChecker):
    name = "Kinetic"

    # Kinetic by Windstream operates in roughly 18 states. Michigan is NOT one of
    # them, so MI addresses are routed away from Kinetic before scraping. Confirm
    # and refine this list against gokinetic.com/locations as coverage shifts.
    coverage_states = frozenset({
        "AL", "AR", "FL", "GA", "IA", "KY", "MN", "MS", "MO", "NE",
        "NM", "NY", "NC", "OH", "OK", "PA", "SC", "TX",
    })

    def __init__(self, headless: bool = True, proxy: str | None = None,
                 pacing: PacingPolicy | None = None):
        self.headless = headless
        self.proxy = proxy
        self.pacing = pacing or PacingPolicy()
        self._session: BrowserSession | None = None

    def _ensure_session(self) -> BrowserSession:
        if self._session is None:
            self._session = launch_session(headless=self.headless, proxy=self.proxy)
        return self._session

    def _rotate(self, attempt: int) -> None:
        self.close()
        self._session = launch_session(headless=self.headless, proxy=self.proxy)

    def check(self, address: AddressInput) -> CheckResult:
        self.pacing.wait_between_requests()

        # Retrying and rotating only helps when there is another proxy identity to
        # rotate to. With no proxy, a blocked IP stays blocked, so one attempt and
        # a clear note beats spawning a fresh browser on every retry.
        if self.proxy is None:
            try:
                return self._attempt(address)
            except Exception as exc:
                return CheckResult(
                    address=address, provider=self.name,
                    category=ResultCategory.UNABLE_TO_VERIFY, raw_status="error",
                    notes=str(exc)[:250],
                )

        return with_retries(self.pacing, lambda: self._attempt(address), on_block=self._rotate)

    def _attempt(self, address: AddressInput) -> CheckResult:
        session = self._ensure_session()
        page = session.page
        api_body = self._drive(page, address)
        verdict = self._page_verdict(page)

        if verdict == "available":
            # If we also caught the API, use it for the exact speed; otherwise
            # the result page itself is enough to call it available.
            if api_body:
                parsed = self._interpret(address, api_body)
                if parsed.category in (ResultCategory.FIBER_AVAILABLE,
                                       ResultCategory.EXISTING_CUSTOMER):
                    return parsed
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.FIBER_AVAILABLE,
                               technology="Fiber", raw_status="dom")
        if verdict == "unavailable":
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.NOT_AVAILABLE, raw_status="dom")
        if api_body:
            return self._interpret(address, api_body)
        raise Blocked("could not read a verdict from the Kinetic result page")

    def _drive(self, page, address: AddressInput) -> str | None:
        """Run the buy flow and return the qualification JSON if we catch it.

        The verdict is read from the result page, which is the source of truth a
        human sees. The API body is a best-effort bonus used to enrich the speed.
        """
        page.goto(BUY_URL, wait_until="domcontentloaded", timeout=45000)
        if any(marker in page.content() for marker in BLOCK_MARKERS):
            raise Blocked("Kinetic challenge on load")

        page.click(ADDRESS_INPUT_SELECTOR)
        for ch in address.single_line():
            page.keyboard.type(ch)
            page.wait_for_timeout(60)
        page.wait_for_timeout(3000)  # let the Precisely autocomplete resolve

        api_body = None
        try:
            # buy.gokinetic plays a "checking availability" map animation for up
            # to ~10s, so give the qualification call room to resolve.
            with page.expect_response(
                lambda r: SEARCH_API_HINT in r.url, timeout=20000
            ) as info:
                self._pick_suggestion(page, address)
                self._click_check(page)
            response = info.value
            if response.status < 400:
                api_body = response.text()
        except Exception:
            pass  # the submit still happened; we read the verdict from the page

        self._wait_for_result(page)
        return api_body

    def _wait_for_result(self, page) -> None:
        """Wait for the result page to actually render a verdict."""
        for _ in range(30):  # up to ~15s past the submit
            if self._page_verdict(page) is not None:
                return
            page.wait_for_timeout(500)

    def _page_verdict(self, page) -> str | None:
        """Read the result page the way a person would. The "this address
        currently has kinetic service" page and a fiber plans page both mean
        available; a "not available / sorry" message means not available."""
        try:
            text = page.inner_text("body").lower()
        except Exception:
            return None
        available = (
            ("kinetic service" in text and ("view plans" in text or "currently has" in text))
            or ("fiber" in text and any(k in text for k in ("add to cart", "gig", "/mo", "great news")))
        )
        if available:
            return "available"
        if any(k in text for k in ("not available", "no service", "sorry",
                                   "isn't available", "not currently", "unable to service")):
            return "unavailable"
        return None

    def _pick_suggestion(self, page, address: AddressInput) -> None:
        """Click the autocomplete row that matches our street address."""
        token = address.address_line1.strip()
        try:
            page.get_by_text(token, exact=False).first.click(timeout=5000)
            return
        except Exception:
            pass
        try:
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(400)
            page.keyboard.press("Enter")
        except Exception:
            pass

    def _click_check(self, page) -> None:
        try:
            page.click(CHECK_BUTTON, timeout=4000)
        except Exception:
            page.keyboard.press("Enter")

    def _interpret(self, address: AddressInput, body: str) -> CheckResult:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.UNABLE_TO_VERIFY,
                               raw_status="unparseable")

        tech = str(data.get("techType", "")).upper()
        max_qual = str(data.get("maxQual", ""))
        speed = _format_speed(_dig(data, "broadbandService", "finalQualSpeed"))
        matched = str(data.get("formattedAddress", "") or data.get("qualAddress", ""))

        if _is_existing_customer(data):
            category = ResultCategory.EXISTING_CUSTOMER
        elif "FIBER" in tech or "FIBER" in max_qual.upper():
            category = ResultCategory.FIBER_AVAILABLE
        elif tech or max_qual:
            category = ResultCategory.NOT_AVAILABLE  # serviceable, but not fiber
        else:
            category = ResultCategory.UNABLE_TO_VERIFY

        return CheckResult(
            address=address, provider=self.name, category=category,
            fiber_speed=speed if category == ResultCategory.FIBER_AVAILABLE else "",
            technology="Fiber" if category == ResultCategory.FIBER_AVAILABLE else tech.title(),
            matched_address=matched,
            raw_status=max_qual or tech or "no_qual",
        )

    def confirm_endpoint(self, address: AddressInput) -> list[dict]:
        """Recon helper: capture the qualification request for this address."""
        session = self._ensure_session()
        captured: list[dict] = []

        def record(request):
            if SEARCH_API_HINT in request.url:
                body = None
                try:
                    body = request.post_data
                except Exception:
                    body = "<binary>"
                captured.append({"url": request.url, "method": request.method, "body": body})

        session.page.on("request", record)
        self._lookup(session, address)
        return captured

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


def _dig(data: dict, *keys):
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _is_existing_customer(data: dict) -> bool:
    flat = json.dumps(data).lower()
    return '"existingcustomer":true' in flat or '"iscustomer":true' in flat


def _format_speed(kbps) -> str:
    try:
        value = int(kbps)
    except (ValueError, TypeError):
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f} Gbps"
    if value >= 1_000:
        return f"{value / 1_000:.0f} Mbps"
    return f"{value} Kbps"
