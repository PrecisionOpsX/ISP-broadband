import unittest

from serviceability.models import CSV_COLUMNS, AddressInput, CheckResult, ResultCategory


class ModelTests(unittest.TestCase):
    def test_address_key_is_stable_and_case_insensitive(self):
        a = AddressInput("123 Main St", "Town", "TX", "75001")
        b = AddressInput("123 MAIN st", "town", "tx", "75001")
        self.assertEqual(a.key(), b.key())

    def test_address_id_overrides_composite_key(self):
        a = AddressInput("123 Main St", "Town", "TX", "75001", address_id="X1")
        self.assertEqual(a.key(), "X1")

    def test_row_has_every_csv_column(self):
        a = AddressInput("123 Main St", "Town", "TX", "75001")
        row = CheckResult(address=a, provider="AT&T",
                          category=ResultCategory.FIBER_AVAILABLE).to_row()
        self.assertEqual(set(row.keys()), set(CSV_COLUMNS))


if __name__ == "__main__":
    unittest.main()
