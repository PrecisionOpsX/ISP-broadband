"""AT&T serviceability checker.

AT&T exposes a JSON availability API, but the whole endpoint sits behind Akamai
Bot Manager, so a raw request gets a 403. The approach that survives repeated
runs is browser-first: warm a real browser on the availability page so Akamai's
JS sets its cookies, then fire the JSON call from inside that page so it carries
the warmed cookies and the browser's TLS fingerprint. We read structured JSON,
not scraped HTML.

The exact response shape shifts over time. Rather than hard-code one field path,
we scan the returned JSON for fiber signals, which keeps this resilient to minor
backend changes. confirm_endpoint() captures the live calls so the exact request
can be locked down during validation on the client's network.
"""

from __future__ import annotations

import json

from ..browser import BrowserSession, launch_session
from ..interface import ProviderChecker
from ..models import AddressInput, CheckResult, ResultCategory
from ..pacing import Blocked, PacingPolicy, with_retries

AVAILABILITY_URL = "https://www.att.com/internet/availability/"
CHECK_ENDPOINT = (
    "https://www.att.com/services/shop/model/ecom/shop/view/unified/"
    "qualification/service/CheckAvailabilityRESTService/invokeCheckAvailability"
)

# Strings that, if present in the Akamai challenge page, mean we were blocked.
BLOCK_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption", "errorpage")

# Snippet that runs the qualification call from inside the warmed page so it
# inherits cookies and fingerprint. Returns status and parsed body to Python.
FETCH_SNIPPET = """
async (payload) => {
    const res = await fetch(payload.url, {
        method: 'POST',
        headers: {'content-type': 'application/json', 'accept': 'application/json'},
        body: JSON.stringify(payload.body),
        credentials: 'include',
    });
    const text = await res.text();
    return {status: res.status, body: text};
}
"""


class AttChecker(ProviderChecker):
    name = "AT&T"

    # AT&T wireline (Internet/Fiber) operates across its legacy 21-state ILEC
    # footprint. Michigan is in it (former Ameritech). Verify and refine as needed.
    coverage_states = frozenset({
        "AL", "AR", "CA", "FL", "GA", "IL", "IN", "KS", "KY", "LA", "MI",
        "MS", "MO", "NV", "NC", "OH", "OK", "SC", "TN", "TX", "WI",
    })

    def __init__(self, headless: bool = True, proxy: str | None = None,
                 pacing: PacingPolicy | None = None, unblocker=None):
        self.headless = headless
        self.proxy = proxy
        self.pacing = pacing or PacingPolicy()
        self.unblocker = unblocker
        self._session: BrowserSession | None = None

    def _launch(self) -> BrowserSession:
        # Route through the commercial unblocker if one is configured, since
        # Akamai blocks plain automation here. Unblocker proxies often MITM TLS,
        # so we ignore cert errors only on that path.
        if self.unblocker is not None and self.unblocker.browser_proxy():
            return launch_session(headless=self.headless,
                                  proxy=self.unblocker.browser_proxy(),
                                  ignore_https_errors=True)
        return launch_session(headless=self.headless, proxy=self.proxy)

    def _ensure_session(self) -> BrowserSession:
        if self._session is None:
            self._session = self._launch()
            self._warm()
        return self._session

    def _warm(self) -> None:
        """Load the availability page so Akamai runs its JS and sets cookies."""
        page = self._session.page
        page.goto(AVAILABILITY_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if self._looks_blocked(page.content()):
            raise Blocked("AT&T challenge on warm-up")

    def _rotate(self, attempt: int) -> None:
        """Burn the current identity and warm a fresh one before retrying."""
        self.close()
        self._session = self._launch()
        self._warm()

    @staticmethod
    def _looks_blocked(text: str) -> bool:
        return any(marker in text for marker in BLOCK_MARKERS)

    def check(self, address: AddressInput) -> CheckResult:
        self.pacing.wait_between_requests()

        def do_check() -> CheckResult:
            session = self._ensure_session()
            payload = {
                "url": CHECK_ENDPOINT,
                "body": {
                    "userInputZip": address.zip_code,
                    "userInputAddressLine1": address.address_line1,
                    "userInputAddressLine2": address.unit,
                    "mode": "fullAddress",
                    "customer_type": "Consumer",
                    "dtvMigrationFlag": False,
                },
            }
            response = session.page.evaluate(FETCH_SNIPPET, payload)
            if response["status"] in (401, 403, 429) or self._looks_blocked(response["body"]):
                raise Blocked(f"AT&T status {response['status']}")
            return self._interpret(address, response["body"])

        result = with_retries(self.pacing, do_check, on_block=self._rotate)
        result.final_url = self._current_url()
        return result

    def _current_url(self) -> str:
        try:
            return self._session.page.url
        except Exception:
            return ""

    def _interpret(self, address: AddressInput, body: str) -> CheckResult:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return CheckResult(
                address=address, provider=self.name,
                category=ResultCategory.UNABLE_TO_VERIFY,
                raw_status="unparseable_response", notes=body[:200],
            )

        flat = json.dumps(data).lower()
        matched = _first_string(data, ("formattedaddress", "matchedaddress", "addressline1"))

        if _truthy(data, "isexistingcustomer"):
            category = ResultCategory.EXISTING_CUSTOMER
        elif _has_fiber_signal(data, flat):
            category = ResultCategory.FIBER_AVAILABLE
        elif _address_recognized(data, flat):
            category = ResultCategory.NOT_AVAILABLE
        else:
            category = ResultCategory.UNABLE_TO_VERIFY

        return CheckResult(
            address=address, provider=self.name, category=category,
            fiber_speed=_first_string(data, ("maxspeed", "downloadspeed", "speed")),
            technology="Fiber" if category == ResultCategory.FIBER_AVAILABLE else "",
            matched_address=matched,
            raw_status=_first_string(data, ("qualificationstatus", "status")) or "ok",
        )

    def confirm_endpoint(self, address: AddressInput) -> list[dict]:
        """Recon helper: drive the real form and capture the XHR it fires.

        Run this once on the validation network to lock the exact current
        request and response shape, then keep check() in sync. Returns the
        captured availability-related requests.
        """
        session = self._ensure_session()
        captured: list[dict] = []

        def record(request):
            url = request.url
            if "availability" in url.lower() or "qualif" in url.lower():
                captured.append({"url": url, "method": request.method,
                                 "post_data": request.post_data})

        session.page.on("request", record)
        session.page.fill("input[type='text']", address.single_line())
        session.page.keyboard.press("Enter")
        session.page.wait_for_timeout(6000)
        return captured

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


def _has_fiber_signal(data: object, flat: str) -> bool:
    if _truthy(data, "isgigafiberavailable") or _truthy(data, "isfiberavailable"):
        return True
    return '"fiber"' in flat and "available" in flat


def _address_recognized(data: object, flat: str) -> bool:
    return "formattedaddress" in flat or "matchedaddress" in flat or "qualification" in flat


def _truthy(data: object, key: str) -> bool:
    found = _find_key(data, key)
    return found in (True, "true", "Y", "yes", 1)


def _first_string(data: object, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _find_key(data, key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _find_key(data: object, target: str):
    """Case-insensitive deep search for the first value under target key."""
    target = target.lower()
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == target:
                    return value
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None
