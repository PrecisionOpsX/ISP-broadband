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
import random
from urllib.parse import urlparse

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

# After submit the flow lands on /check-in-progress (a loading page) and then
# redirects to a result page whose URL path is the verdict. Confirmed mappings.
LOADING_PATH = "check-in-progress"
RESULT_PAGES = [
    ("email-collection", ResultCategory.FIBER_AVAILABLE, "Fiber", "available, email collection step"),
    ("existing-account", ResultCategory.EXISTING_CUSTOMER, "", "address already has Kinetic service"),
    ("complete-address", ResultCategory.UNABLE_TO_VERIFY, "", "address incomplete or zip/state mismatch"),
]


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
        path = urlparse(page.url).path.lower()

        # The result page URL is the verdict. Map the confirmed ones first.
        for marker, category, technology, note in RESULT_PAGES:
            if marker in path:
                if category == ResultCategory.FIBER_AVAILABLE and api_body:
                    parsed = self._interpret(address, api_body)
                    if parsed.category == ResultCategory.FIBER_AVAILABLE:
                        parsed.notes = note
                        return parsed
                return CheckResult(address=address, provider=self.name,
                                   category=category, technology=technology,
                                   raw_status=marker, notes=note)

        # Unknown result page: read its content, then fall back to the API.
        verdict = self._page_verdict(page)
        if verdict == "available":
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.FIBER_AVAILABLE,
                               technology="Fiber", raw_status=path or "dom")
        if verdict == "unavailable":
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.NOT_AVAILABLE, raw_status=path or "dom")
        if api_body:
            return self._interpret(address, api_body)
        raise Blocked(f"unrecognized Kinetic result page: /{path.strip('/') or 'no-redirect'}")

    def _drive(self, page, address: AddressInput) -> str | None:
        """Run the buy flow, wait for the result redirect, and return the
        qualification JSON if we catch it (a best-effort bonus for the speed)."""
        page.goto(BUY_URL, wait_until="domcontentloaded", timeout=45000)
        if any(marker in page.content() for marker in BLOCK_MARKERS):
            raise Blocked("Kinetic challenge on load")

        # Kinetic scores the session with reCAPTCHA and shunts anything that looks
        # automated to a /call-ris page. Behaving like a person (real mouse moves,
        # real clicks, human typing cadence) is what keeps us on the real flow.
        self._wander(page)
        self._mouse_click(page, page.locator(ADDRESS_INPUT_SELECTOR).first)
        self._human_type(page, address.single_line())
        page.wait_for_timeout(random.randint(2600, 3800))  # autocomplete resolves

        api_body = None
        try:
            with page.expect_response(
                lambda r: SEARCH_API_HINT in r.url, timeout=20000
            ) as info:
                self._pick_suggestion(page, address)
                self._click_check(page)
            response = info.value
            if response.status < 400:
                api_body = response.text()
        except Exception:
            pass  # the submit still happened; the verdict comes from the redirect

        self._wait_for_result_page(page)
        return api_body

    def _wait_for_result_page(self, page) -> None:
        """Wait for the redirect off /check-in-progress to a result page.

        /check-in-progress is just a loading screen and can take a while, so we
        wait for it to redirect. As soon as a real result path appears we stop,
        so a fast redirect is not slowed down by a fixed sleep.
        """
        for _ in range(80):  # up to ~40s for a slow qualification
            path = urlparse(page.url).path.strip("/").lower()
            if path and path != LOADING_PATH:
                page.wait_for_timeout(600)  # let the result page settle
                return
            page.wait_for_timeout(500)

    def _page_verdict(self, page) -> str | None:
        """Content fallback for a result page whose URL we do not recognize."""
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
            self._mouse_click(page, page.get_by_text(token, exact=False).first)
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
            self._mouse_click(page, page.locator(CHECK_BUTTON).first)
        except Exception:
            page.keyboard.press("Enter")

    def _human_type(self, page, text: str) -> None:
        """Type with a varied cadence rather than a fixed machine rhythm."""
        for ch in text:
            page.keyboard.type(ch)
            page.wait_for_timeout(random.randint(45, 150))

    def _mouse_click(self, page, locator) -> None:
        """Move the mouse to an element and click it, the way a person would."""
        locator.scroll_into_view_if_needed(timeout=5000)
        box = locator.bounding_box()
        if not box:
            locator.click(timeout=5000)
            return
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        page.mouse.move(x, y, steps=random.randint(6, 14))
        page.wait_for_timeout(random.randint(120, 360))
        page.mouse.click(x, y)

    def _wander(self, page) -> None:
        """A little aimless mouse movement before interacting, like a human."""
        for _ in range(random.randint(2, 4)):
            page.mouse.move(random.randint(200, 1000), random.randint(180, 600),
                            steps=random.randint(5, 12))
            page.wait_for_timeout(random.randint(150, 450))

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
