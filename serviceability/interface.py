"""The contract every provider checker implements.

Adding a provider means writing one class that satisfies this interface. Nothing
else in the system changes. If a provider alters its flow, we fix that one class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AddressInput, CheckResult, ResultCategory


class ProviderChecker(ABC):
    """Base class for a single provider's serviceability checker."""

    # Display name stored on every result row. Use the brand the client tracks.
    name: str = "unknown"

    # USPS state codes this provider actually operates in. Empty means "check
    # everywhere". Used to route addresses before scraping so we never spend a
    # lookup (or an unblocker request) on a state the provider does not serve.
    coverage_states: frozenset = frozenset()

    def serves(self, address: AddressInput) -> bool:
        """Whether this provider could plausibly serve the address at all.

        State-level routing is the coarse first pass. It is deliberately
        permissive: it only rules out clear out-of-footprint addresses, never
        confirms availability. The live check is still the source of truth.
        """
        if not self.coverage_states:
            return True
        return address.state.strip().upper() in self.coverage_states

    @abstractmethod
    def check(self, address: AddressInput) -> CheckResult:
        """Check one address and return a normalized result.

        Implementations must never raise for an ordinary "could not determine"
        outcome. They should return UNABLE_TO_VERIFY so a transient site problem
        for one address does not abort a run of thousands.
        """
        raise NotImplementedError

    def check_many(self, addresses: list[AddressInput]) -> list[CheckResult]:
        """Default sequential implementation. Workers parallelize at a higher level."""
        return [self._safe_check(addr) for addr in addresses]

    def _safe_check(self, address: AddressInput) -> CheckResult:
        try:
            return self.check(address)
        except Exception as exc:  # one bad address must not kill the batch
            return CheckResult(
                address=address,
                provider=self.name,
                category=ResultCategory.UNABLE_TO_VERIFY,
                raw_status="checker_exception",
                notes=str(exc)[:300],
            )

    def close(self) -> None:
        """Release any browser or network resources. Safe to call more than once."""
