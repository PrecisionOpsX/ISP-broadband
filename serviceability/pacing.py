"""Request pacing and retry-with-backoff.

This is the heart of staying unblocked. Pacing keeps us under the radar, backoff
turns a challenge response into "slow down and rotate" rather than a hammering
loop that gets the whole identity banned.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass
class PacingPolicy:
    """Tunable timing. Conservative defaults that the full system can relax."""

    min_delay_seconds: float = 4.0
    max_delay_seconds: float = 9.0
    max_retries: int = 4
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 90.0

    def wait_between_requests(self) -> None:
        time.sleep(random.uniform(self.min_delay_seconds, self.max_delay_seconds))

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        ceiling = min(self.backoff_cap_seconds, self.backoff_base_seconds * (2 ** attempt))
        return random.uniform(0, ceiling)


class Blocked(Exception):
    """Raised by a provider call when the site challenged or blocked us."""


def with_retries(policy: PacingPolicy, call, on_block=None):
    """Run call(), retrying on Blocked with backoff.

    on_block, if given, runs before each retry so the provider can rotate proxy
    or re-warm its browser session instead of retrying the same dead identity.
    """
    last_error: Exception | None = None
    for attempt in range(policy.max_retries):
        try:
            return call()
        except Blocked as exc:
            last_error = exc
            if on_block is not None:
                on_block(attempt)
            time.sleep(policy.backoff_delay(attempt))
    raise Blocked(f"still blocked after {policy.max_retries} attempts: {last_error}")
