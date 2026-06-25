"""Normalized data model shared by every provider checker.

Each provider site speaks its own language. The whole point of this module is
that AT&T booleans and Kinetic technology strings both collapse into the same
small set of categories and the same row shape, so storage, comparison, and CSV
never need to know which provider produced a result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ResultCategory(str, Enum):
    """The client's category set, per address per provider.

    The first four are produced by live site checks. The last two are
    cross-reference verdicts that need FCC Fabric data and are populated by the
    comparison layer in the full system, not by the live checkers.
    """

    FIBER_AVAILABLE = "Fiber Available"
    NOT_AVAILABLE = "Not Available"
    EXISTING_CUSTOMER = "Existing Customer"
    UNABLE_TO_VERIFY = "Unable to Verify"
    ON_FCC_NOT_LIVE = "On FCC but not live"
    LIVE_NOT_ON_FCC = "Live on provider site but not on FCC"


# Categories that count as "fiber is serviceable here right now" for the
# purpose of detecting a Not Available to Available flip.
AVAILABLE_CATEGORIES = frozenset({ResultCategory.FIBER_AVAILABLE})


@dataclass(frozen=True)
class AddressInput:
    """One address to check. Kept provider-agnostic on purpose."""

    address_line1: str
    city: str
    state: str
    zip_code: str
    address_id: str = ""
    unit: str = ""

    def key(self) -> str:
        """Stable identity for this address across runs."""
        if self.address_id:
            return self.address_id
        parts = [self.address_line1, self.unit, self.city, self.state, self.zip_code]
        return "|".join(p.strip().lower() for p in parts if p.strip())

    def single_line(self) -> str:
        bits = [self.address_line1]
        if self.unit:
            bits.append(self.unit)
        bits.append(f"{self.city}, {self.state} {self.zip_code}")
        return ", ".join(bits)


@dataclass
class CheckResult:
    """One normalized serviceability result for one address and one provider."""

    address: AddressInput
    provider: str
    category: ResultCategory
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Optional richer detail the providers expose. Useful for the client and for
    # debugging, but the category is what the comparison engine acts on.
    fiber_speed: str = ""
    technology: str = ""
    matched_address: str = ""
    raw_status: str = ""
    notes: str = ""

    def to_row(self) -> dict:
        """Flatten to the unified CSV/database row shape."""
        return {
            "address_key": self.address.key(),
            "address_line1": self.address.address_line1,
            "unit": self.address.unit,
            "city": self.address.city,
            "state": self.address.state,
            "zip": self.address.zip_code,
            "provider": self.provider,
            "category": self.category.value,
            "fiber_speed": self.fiber_speed,
            "technology": self.technology,
            "matched_address": self.matched_address,
            "raw_status": self.raw_status,
            "notes": self.notes,
            "checked_at": self.checked_at.astimezone(timezone.utc).isoformat(),
        }


# Column order for the unified CSV. The full system reuses this exact header.
CSV_COLUMNS = [
    "address_key",
    "address_line1",
    "unit",
    "city",
    "state",
    "zip",
    "provider",
    "category",
    "fiber_speed",
    "technology",
    "matched_address",
    "raw_status",
    "notes",
    "checked_at",
]
