"""Snapshot comparison engine: the most important deliverable.

It diffs the latest run against history, per address per provider, and surfaces
the addresses that flipped from Not Available to Available. Those flips are the
client's product: fresh fiber leads. Fresh buildout does not show on the FCC map,
so a live flip is the source of truth that something new went live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from .models import AVAILABLE_CATEGORIES, CheckResult, ResultCategory
from .storage.db import ResultStore


def _is_available(category_value: str) -> bool:
    try:
        return ResultCategory(category_value) in AVAILABLE_CATEGORIES
    except ValueError:
        return False


@dataclass
class FreshLead:
    """One address that gained fiber since we last saw it."""

    address_key: str
    provider: str
    previous_category: str
    current_category: str
    matched_address: str
    fiber_speed: str
    first_seen_available: str  # checked_at of the run that found it available

    def to_row(self) -> dict:
        return {
            "address_key": self.address_key,
            "provider": self.provider,
            "previous_category": self.previous_category,
            "current_category": self.current_category,
            "matched_address": self.matched_address,
            "fiber_speed": self.fiber_speed,
            "first_seen_available": self.first_seen_available,
        }


def find_fresh_leads(store: ResultStore, current_run: list[CheckResult]) -> list[FreshLead]:
    """Compare this run against each address's prior known state.

    A lead is fresh when the prior state was a definite Not Available and the
    current state is Available. We deliberately do not treat Unable to Verify or
    a missing prior state as Not Available, so a flaky earlier check cannot
    manufacture a false lead.
    """
    leads: list[FreshLead] = []
    by_provider: dict[str, list[CheckResult]] = {}
    for result in current_run:
        by_provider.setdefault(result.provider, []).append(result)

    for provider, results in by_provider.items():
        run_start = min(r.checked_at for r in results).astimezone(timezone.utc).isoformat()
        history = store.latest_per_address(provider, before=run_start)
        for result in results:
            if result.category not in AVAILABLE_CATEGORIES:
                continue
            prior = history.get(result.address.key())
            if prior is None:
                continue  # first time we have seen this address, not a flip
            if prior["category"] == ResultCategory.NOT_AVAILABLE.value:
                leads.append(
                    FreshLead(
                        address_key=result.address.key(),
                        provider=provider,
                        previous_category=prior["category"],
                        current_category=result.category.value,
                        matched_address=result.matched_address,
                        fiber_speed=result.fiber_speed,
                        first_seen_available=result.checked_at.astimezone(timezone.utc).isoformat(),
                    )
                )
    return leads
