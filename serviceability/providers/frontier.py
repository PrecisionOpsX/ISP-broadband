"""Frontier serviceability checker.

Flow confirmed by hand-testing the real site:

1. Open frontier.com/buy directly and close any popup. It has its own address
   field and a "Check Availability" button.
2. Paste the whole address at once so the autocomplete API fires a single time,
   then wait through the "Loading..." state until the dropdown opens.
3. If no suggestion exactly matches the typed address (same house number, city,
   state, zip), the address is not recognized, so it is Unable to Verify.
4. If a suggestion matches, select it and click Check Availability.
5. The /buy page shows the result. Read which result it is:
   - "currently has Frontier service" / "View plans" / "Are you moving to this
     address" -> Fiber Available
   - "not available at this address" / Allconnect referral -> Not Available
   - "experiencing technical problem" -> refresh /buy and re-enter, up to 3 tries

Frontier runs on Verizon-legacy infrastructure and serves an "Access denied,
Verizon Information Security Policy" page to traffic it does not like. Deep-linking
straight to /buy trips it, so we always enter from the homepage.
"""

from __future__ import annotations

import random
import re

from ..browser import BrowserSession, launch_session
from ..interface import ProviderChecker
from ..models import AddressInput, CheckResult, ResultCategory
from ..pacing import Blocked, PacingPolicy, with_retries

BUY_URL = "https://frontier.com/buy"
ADDRESS_INPUT = ("input[name='street-address'], input[placeholder*='address' i], "
                 "input[id*='address' i]")
GO_BUTTON = ("button:has-text('Check Availability'), button:has-text('Go'), "
             "button:has-text('GO')")
POPUP_CLOSE = ("button:has-text('Close')", "button[aria-label*='close' i]",
               "[role=dialog] button[aria-label*='close' i]",
               "button:has-text('No thanks')", "button:has-text('Maybe later')")
SERVICE_API_HINT = "/ol/api/v2/serviceability"

BLOCK_MARKERS = ("access denied", "verizon information security",
                 "pardon our interruption", "request unsuccessful")
AVAILABLE_MARKERS = ("currently has frontier service", "view plans",
                     "are you moving to this address")
NOT_AVAILABLE_MARKERS = ("not available at this address", "internet is not available",
                         "we're sorry", "allconnect")
TECHNICAL_MARKERS = ("experiencing technical", "technical problem",
                     "technical difficult", "something went wrong")


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
        if self.proxy is None:
            try:
                result = self._attempt(address)
            except Exception as exc:
                result = CheckResult(address=address, provider=self.name,
                                     category=ResultCategory.UNABLE_TO_VERIFY,
                                     raw_status="error", notes=str(exc)[:250])
        else:
            result = with_retries(self.pacing, lambda: self._attempt(address),
                                  on_block=self._rotate)
        result.final_url = self._current_url()
        return result

    def _attempt(self, address: AddressInput) -> CheckResult:
        session = self._ensure_session()
        page = session.page
        line1 = address.address_line1.strip()
        if not line1 or not line1[0].isdigit():
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.UNABLE_TO_VERIFY,
                               raw_status="no_house_number",
                               notes="address has no house number to match")

        self._open_buy(page)
        if not self._type_and_select(page, address):
            return CheckResult(address=address, provider=self.name,
                               category=ResultCategory.UNABLE_TO_VERIFY,
                               raw_status="incorrect_address",
                               notes="no matching address in the Frontier autocomplete")
        return self._read_buy(page, address)

    def _open_buy(self, page) -> None:
        self._safe_goto(page, BUY_URL)
        if self._blocked(page):
            raise Blocked("Frontier access blocked (Verizon security policy)")
        self._close_popups(page)
        try:
            page.wait_for_selector(ADDRESS_INPUT, timeout=8000)
        except Exception:
            raise Blocked("Frontier address input not found (possible block)")

    def _type_and_select(self, page, address: AddressInput) -> bool:
        """Type the address, and select a dropdown suggestion only if it exactly
        matches. Returns False when nothing matches (an unrecognized address)."""
        inp = page.locator(ADDRESS_INPUT).first
        try:
            inp.scroll_into_view_if_needed(timeout=4000)
            inp.click(timeout=4000)
            # Paste the whole address at once so the autocomplete API fires a
            # single time, rather than on every keystroke.
            inp.fill(address.single_line())
        except Exception:
            return False
        match = self._wait_for_match(page, address)
        if match is None:
            return False

        try:
            self._mouse_click(page, match)
        except Exception:
            try:
                match.click(timeout=3000)
            except Exception:
                return False
        page.wait_for_timeout(1000)
        try:
            page.click(GO_BUTTON, timeout=5000)
        except Exception:
            page.keyboard.press("Enter")
        return True

    def _read_buy(self, page, address: AddressInput) -> CheckResult:
        """Read the /buy result, refreshing and re-entering on a technical error
        up to three times, as the manual flow does."""
        for _ in range(3):
            state = self._await_buy_state(page)
            if state == "available":
                return CheckResult(address=address, provider=self.name,
                                   category=ResultCategory.FIBER_AVAILABLE,
                                   technology="Fiber", raw_status="buy_available")
            if state == "unavailable":
                return CheckResult(address=address, provider=self.name,
                                   category=ResultCategory.NOT_AVAILABLE,
                                   raw_status="buy_not_available")
            # technical error or nothing rendered: refresh /buy and re-enter
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            self._close_popups(page)
            if not self._reenter_on_buy(page, address):
                break
        return CheckResult(address=address, provider=self.name,
                           category=ResultCategory.UNABLE_TO_VERIFY,
                           raw_status="no_result",
                           notes="no Frontier result after retries")

    def _reenter_on_buy(self, page, address: AddressInput) -> bool:
        try:
            page.wait_for_selector(ADDRESS_INPUT, timeout=6000)
        except Exception:
            return False
        return self._type_and_select(page, address)

    def _await_buy_state(self, page) -> str | None:
        for _ in range(30):  # up to ~15s for the result to render
            try:
                text = page.inner_text("body").lower()
            except Exception:
                text = ""
            if any(m in text for m in NOT_AVAILABLE_MARKERS):
                return "unavailable"
            if any(m in text for m in AVAILABLE_MARKERS):
                return "available"
            if any(m in text for m in TECHNICAL_MARKERS):
                return "technical"
            page.wait_for_timeout(500)
        return None

    def _wait_for_match(self, page, address: AddressInput):
        """Wait for the autocomplete dropdown to populate (it loads from an API,
        so it lags the keystrokes), then return the element that exactly matches
        the typed address, or None if the populated list has no match."""
        # The dropdown shows a "Loading..." state first (no address rows), so we
        # poll until real address rows appear, which means loading has finished.
        suggestions = []
        for _ in range(30):  # up to ~15s for the API-backed dropdown to load
            suggestions = self._suggestions(page)
            if suggestions:
                break
            page.wait_for_timeout(500)
        if not suggestions:
            return None
        page.wait_for_timeout(900)  # let the rest of the list settle
        for text, element in self._suggestions(page):
            if self._exact_match(text, address):
                return element
        return None

    def _suggestions(self, page):
        """Return (text, element) for each address-like row in the dropdown."""
        selector = ("li, [role=option], ul li, [class*=suggest i] li, "
                    "[class*=result i] li, [class*=autocomplete i] *")
        out, seen = [], set()
        for element in page.query_selector_all(selector):
            try:
                text = (element.inner_text() or "").strip()
            except Exception:
                continue
            if not text or len(text) > 90 or text in seen:
                continue
            if any(c.isdigit() for c in text):
                seen.add(text)
                out.append((text, element))
        return out[:20]

    @staticmethod
    def _exact_match(suggestion: str, address: AddressInput) -> bool:
        """A suggestion matches when the house number, zip, city, and a street
        word all appear in it. House number is matched as a whole token so 2080
        does not match 20800."""
        s = re.sub(r"[^a-z0-9 ]", " ", suggestion.lower())
        tokens = set(s.split())
        line1 = re.sub(r"[^a-z0-9 ]", " ", address.address_line1.lower()).split()
        if not line1:
            return False
        house = line1[0]
        street_words = [w for w in line1[1:] if len(w) > 2]
        zip5 = address.zip_code.strip()[:5]
        city = address.city.strip().lower()
        if house not in tokens:
            return False
        if zip5 and zip5 not in s:
            return False
        if city and city not in s:
            return False
        if street_words and not any(w in s for w in street_words):
            return False
        return True

    def _close_popups(self, page) -> None:
        for selector in POPUP_CLOSE:
            try:
                loc = page.locator(selector).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500)
                    page.wait_for_timeout(400)
            except Exception:
                continue

    def _safe_goto(self, page, url: str) -> None:
        """frontier.com occasionally aborts its own load when reused between
        addresses. That is transient, so retry a couple of times."""
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

    @staticmethod
    def _blocked(page) -> bool:
        try:
            return any(marker in page.content().lower() for marker in BLOCK_MARKERS)
        except Exception:
            return False

    def _mouse_click(self, page, target) -> None:
        try:
            target.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        box = target.bounding_box()
        if not box:
            target.click(timeout=4000)
            return
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        page.mouse.move(x, y, steps=random.randint(6, 12))
        page.wait_for_timeout(random.randint(120, 300))
        page.mouse.click(x, y)

    def _current_url(self) -> str:
        try:
            return self._session.page.url
        except Exception:
            return ""

    def confirm_endpoint(self, address: AddressInput) -> list[dict]:
        """Recon helper: capture the serviceability requests for this address."""
        session = self._ensure_session()
        captured: list[dict] = []

        def record(request):
            if SERVICE_API_HINT in request.url:
                try:
                    body = request.post_data
                except Exception:
                    body = "<binary>"
                captured.append({"url": request.url, "method": request.method, "body": body})

        session.page.on("request", record)
        self._attempt(address)
        return captured

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
