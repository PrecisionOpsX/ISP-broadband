"""Tests for the comparison engine, the most important deliverable.

A fresh lead must mean a real Not Available to Available flip, and nothing else.
These tests pin that behavior so the full system inherits it.
"""

import os
import tempfile
import unittest

from serviceability.compare import find_fresh_leads
from serviceability.models import AddressInput, CheckResult, ResultCategory
from serviceability.storage.db import ResultStore


def addr(key: str) -> AddressInput:
    return AddressInput(address_line1=key, city="Town", state="TX", zip_code="00000",
                        address_id=key)


def result(key: str, category: ResultCategory, provider: str = "AT&T") -> CheckResult:
    return CheckResult(address=addr(key), provider=provider, category=category)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ResultStore(os.path.join(self.tmp, "t.db"))

    def test_flip_not_available_to_available_is_a_lead(self):
        self.store.save([result("a", ResultCategory.NOT_AVAILABLE)])
        current = [result("a", ResultCategory.FIBER_AVAILABLE)]
        leads = find_fresh_leads(self.store, current)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].address_key, "a")

    def test_first_ever_sighting_is_not_a_lead(self):
        current = [result("new", ResultCategory.FIBER_AVAILABLE)]
        self.assertEqual(find_fresh_leads(self.store, current), [])

    def test_stays_available_is_not_a_lead(self):
        self.store.save([result("a", ResultCategory.FIBER_AVAILABLE)])
        current = [result("a", ResultCategory.FIBER_AVAILABLE)]
        self.assertEqual(find_fresh_leads(self.store, current), [])

    def test_unable_to_verify_prior_does_not_manufacture_a_lead(self):
        self.store.save([result("a", ResultCategory.UNABLE_TO_VERIFY)])
        current = [result("a", ResultCategory.FIBER_AVAILABLE)]
        self.assertEqual(find_fresh_leads(self.store, current), [])

    def test_providers_are_isolated(self):
        self.store.save([result("a", ResultCategory.NOT_AVAILABLE, provider="AT&T")])
        current = [result("a", ResultCategory.FIBER_AVAILABLE, provider="Kinetic")]
        # Kinetic has no prior history for this address, so no false lead.
        self.assertEqual(find_fresh_leads(self.store, current), [])


if __name__ == "__main__":
    unittest.main()
