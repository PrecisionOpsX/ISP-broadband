"""Unified CSV output. The full system reuses this exact format."""

from __future__ import annotations

import csv

from ..compare import FreshLead
from ..models import CSV_COLUMNS, CheckResult

FRESH_LEAD_COLUMNS = [
    "address_key",
    "provider",
    "previous_category",
    "current_category",
    "matched_address",
    "fiber_speed",
    "first_seen_available",
]


def write_results(path: str, results: list[CheckResult]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(result.to_row())


def write_fresh_leads(path: str, leads: list[FreshLead]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FRESH_LEAD_COLUMNS)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.to_row())
