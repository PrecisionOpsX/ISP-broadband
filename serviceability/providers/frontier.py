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

HOME_URL = "https://frontier.com/"
ADDRESS_INPUT_SELECTOR = "input[name='street-address']"
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
            bodies, page = self._submit(session, address)
            for body in bodies:
                verdict = self._interpret_json(address, body)
                if verdict is not None:
                    return verdict
            return self._interpret_dom(address, page.content())

        return with_retries(self.pacing, do_check, on_block=self._rotate)

    def _submit(self, session: BrowserSession, address: AddressInput):
        page = session.page
        captured: list[str] = []

        def record(response):
            if SERVICE_API_HINT in response.url:
                try:
                    captured.append(response.text())
                except Exception:
                    pass

        page.on("response", record)
        self._safe_goto(page, HOME_URL)
        if any(marker in page.content() for marker in BLOCK_MARKERS):
            raise Blocked("Frontier challenge on load")

        page.click(ADDRESS_INPUT_SELECTOR)
        for ch in address.single_line():
            page.keyboard.type(ch)
            page.wait_for_timeout(60)
        page.wait_for_timeout(3000)
        self._pick_suggestion(page, address)
        self._submit_button(page)
        for _ in range(24):
            if captured:
                break
            page.wait_for_timeout(500)
        return captured, page

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

    def _pick_suggestion(self, page, address: AddressInput) -> None:
        try:
            page.get_by_text(address.address_line1.strip(), exact=False).first.click(timeout=5000)
        except Exception:
            try:
                page.keyboard.press("ArrowDown")
                page.wait_for_timeout(400)
                page.keyboard.press("Enter")
            except Exception:
                pass

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
