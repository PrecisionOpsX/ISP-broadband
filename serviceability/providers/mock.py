"""Offline mock checker for testing the pipeline without hitting any site.

This exists so the storage, comparison, and CSV layers can be exercised end to
end, and so the flip detection can be demonstrated deterministically: point it at
a different "world" file on a second run and watch addresses go Not Available to
Available. It is a test and demo aid, not part of a real run.
"""

from __future__ import annotations

from ..interface import ProviderChecker
from ..models import AddressInput, CheckResult, ResultCategory


class MockChecker(ProviderChecker):
    def __init__(self, name: str, available_keys: set[str] | None = None):
        self.name = name
        self.available_keys = available_keys or set()

    def check(self, address: AddressInput) -> CheckResult:
        if address.key() in self.available_keys:
            return CheckResult(
                address=address, provider=self.name,
                category=ResultCategory.FIBER_AVAILABLE,
                technology="Fiber", fiber_speed="1000", raw_status="mock",
            )
        return CheckResult(
            address=address, provider=self.name,
            category=ResultCategory.NOT_AVAILABLE, raw_status="mock",
        )
