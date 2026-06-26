"""Frontier serviceability checker.

Confirmed by live inspection: the frontier.com address box resolves an address
through frontier.com/ol/api/v2/serviceability/predictive (which returns an
addressKey), then the flow lands on /buy where the qualified plans render. Fiber
service shows as fiber plans and gig speeds; an unserviceable address shows a
"not available" message instead.

We drive that flow and read the serviceability response, with a DOM fallback on
the /buy page. The exact qualification field can be locked on the first clean-IP
run with confirm_endpoint(); the rest of the system never changes.
"""

from __future__ import annotations

import json

from ..browser import BrowserSession, launch_session
from ..interface import ProviderChecker
from ..models import AddressInput, CheckResult, ResultCategory
from ..pacing import Blocked, PacingPolicy, with_retries

BUY_URL = "https://frontier.com/buy"
HOME_URL = "https://frontier.com/"
ADDRESS_INPUT_SELECTOR = ("input[name='street-address'], input[placeholder*='address' i], "
                          "input[id*='street-address' i]")
SUBMIT_BUTTON = ("button:has-text('Check Availability'), button:has-text('Availability'), "
                 "button[type='submit']")
SERVICE_API_HINT = "/ol/api/v2/serviceability"
BLOCK_MARKERS = ("Access Denied", "Pardon Our Interruption", "Request unsuccessful")


class FrontierChecker(ProviderChecker):
    name = "Frontier"

    # Frontier operates across roughly 25 states (legacy GTE/Verizon territories),
    # Michigan included. Refine against frontier.com coverage as needed.
    coverage_states = frozenset({
        "AL", "AZ", "CA", "CT", "FL", "GA", "IL", "IN", "IA", "MI", "MN",
        "MS", "NE", "NV", "NM", "NY", "NC", "OH", "PA", "SC", "TN", "TX",
        "UT", "WA", "WV", "WI",
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

        def do_check() -> CheckResult:
            session = self._ensure_session()
            line1 = address.address_line1.strip()
            if not line1 or not line1[0].isdigit():
                return CheckResult(address=address, provider=self.name,
                                   category=ResultCategory.UNABLE_TO_VERIFY,
                                   raw_status="no_house_number",
                                   notes="address has no house number to match")
            bodies, matched = self._submit(session, address)
            if not matched:
                # No dropdown suggestion matched the typed address, so Frontier
                # has no serviceable record for it.
                return CheckResult(address=address, provider=self.name,
                                   category=ResultCategory.NOT_AVAILABLE,
                                   raw_status="no_match",
                                   notes="no matching Frontier address in the lookup")
            for body in bodies:
                verdict = self._interpret_json(address, body)
                if verdict is not None:
                    return verdict
            return self._interpret_dom(address, session.page.content())

        result = with_retries(self.pacing, do_check, on_block=self._rotate)
        result.final_url = self._current_url()
        return result

    def _current_url(self) -> str:
        try:
            return self._session.page.url
        except Exception:
            return ""

    def _submit(self, session: BrowserSession, address: AddressInput):
        """Type the address and only proceed if a dropdown suggestion actually
        matches it. Returns (captured serviceability bodies, matched flag)."""
        page = session.page
        captured: list[str] = []

        def record(response):
            if SERVICE_API_HINT in response.url:
                try:
                    captured.append(response.text())
                except Exception:
                    pass

        page.on("response", record)
        self._open_address_page(page)

        page.click(ADDRESS_INPUT_SELECTOR)
        for ch in address.single_line():
            page.keyboard.type(ch)
            page.wait_for_timeout(55)
        page.wait_for_timeout(3000)  # let the suggestion dropdown populate

        match = self._matching_suggestion(page, address)
        if match is None:
            return captured, False
        try:
            match.click(timeout=5000)
        except Exception:
            return captured, False

        self._submit_button(page)
        for _ in range(24):
            if captured:
                break
            page.wait_for_timeout(500)
        return captured, True

    def _open_address_page(self, page) -> None:
        """Open Frontier's address-entry page, preferring /buy and falling back
        to the homepage. Raise if neither shows the address input."""
        for url in (BUY_URL, HOME_URL):
            self._safe_goto(page, url)
            if any(marker in page.content() for marker in BLOCK_MARKERS):
                raise Blocked("Frontier challenge on load")
            try:
                page.wait_for_selector(ADDRESS_INPUT_SELECTOR, timeout=6000)
                return
            except Exception:
                continue
        raise Blocked("Frontier address input not found")

    def _matching_suggestion(self, page, address: AddressInput):
        """Return the dropdown element that matches the typed address, or None.

        A match must contain the house number and a street-name word, so a
        nonexistent or out-of-area address (which the dropdown does not list) is
        never silently resolved to some other address.
        """
        tokens = address.address_line1.lower().split()
        if not tokens:
            return None
        house = tokens[0]
        street_words = [w for w in tokens[1:] if len(w) > 2]
        selector = ("li, [role=option], [class*=suggest i], [class*=result i], "
                    "[class*=typeahead i] *, [class*=autocomplete i] *")
        seen = set()
        for element in page.query_selector_all(selector):
            try:
                text = element.inner_text().lower().strip()
            except Exception:
                continue
            if not text or len(text) > 140 or text in seen:
                continue
            seen.add(text)
            if house in text and (not street_words or any(w in text for w in street_words)):
                return element
        return None

    def _safe_goto(self, page, url: str) -> None:
        """frontier.com occasionally aborts its own load when the page is reused
        between addresses. That is transient, so retry a couple of times."""
        last = None
        for _ in range(3):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return
            except Exception as exc:
                last = exc
                if "ERR_ABORTED" in str(exc) or "navigation" in str(exc).lower():
                    page.wait_for_timeout(1500)
                    continue
                raise
        raise Blocked(f"Frontier navigation kept aborting: {last}")

    def _submit_button(self, page) -> None:
        try:
            page.click(SUBMIT_BUTTON, timeout=4000)
        except Exception:
            page.keyboard.press("Enter")

    def _interpret_json(self, address: AddressInput, body: str) -> CheckResult | None:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return None
        flat = json.dumps(data).lower()
        if "fiber" not in flat and "serviceab" not in flat and "footprint" not in flat:
            return None

        has_fiber = "fiber" in flat and ("available" in flat or "eligible" in flat
                                         or "true" in flat or "qualified" in flat)
        if has_fiber:
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.FIBER_AVAILABLE,
                               technology="Fiber", raw_status="json")
        if '"infootprint":false' in flat or '"serviceable":false' in flat:
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.NOT_AVAILABLE, raw_status="json")
        return None

    def _interpret_dom(self, address: AddressInput, html: str) -> CheckResult:
        text = html.lower()
        if "fiber" in text and ("add to cart" in text or "gig" in text or "/mo" in text):
            category, tech = ResultCategory.FIBER_AVAILABLE, "Fiber"
        elif "not available" in text or "not currently" in text or "sorry" in text:
            category, tech = ResultCategory.NOT_AVAILABLE, ""
        else:
            category, tech = ResultCategory.UNABLE_TO_VERIFY, ""
        return CheckResult(address=address, provider=self.name, category=category,
                           technology=tech, raw_status="dom")

    def confirm_endpoint(self, address: AddressInput) -> list[dict]:
        """Recon helper: capture the serviceability requests for this address."""
        session = self._ensure_session()
        captured: list[dict] = []

        def record(request):
            if SERVICE_API_HINT in request.url:
                body = None
                try:
                    body = request.post_data
                except Exception:
                    body = "<binary>"
                captured.append({"url": request.url, "method": request.method, "body": body})

        session.page.on("request", record)
        self._submit(session, address)
        return captured

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
