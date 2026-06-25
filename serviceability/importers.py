"""Adapters that turn a client's raw address export into AddressInput rows.

Clients hand us whatever their lead tool produces. Each format gets one small
parser here, and the rest of the system never sees the raw shape. DealMachine is
the first format the client uses.
"""

from __future__ import annotations

import csv
import re

from .models import AddressInput

_STATE_ZIP = re.compile(r"^([A-Za-z]{2})\s+(\d{5})(?:-\d{4})?$")


def parse_full_address(full: str) -> tuple[str, str, str, str]:
    """Split "123 Main St, Davison, MI 48423" into line1, city, state, zip.

    Returns empty strings for parts we cannot read rather than guessing, so a
    malformed row degrades to Unable to Verify instead of a wrong lookup.
    """
    parts = [p.strip() for p in full.split(",") if p.strip()]
    if len(parts) < 3:
        return full.strip(), "", "", ""
    tail = parts[-1]
    match = _STATE_ZIP.match(tail)
    if not match:
        return full.strip(), "", "", ""
    state, zip_code = match.group(1).upper(), match.group(2)
    city = parts[-2]
    line1 = ", ".join(parts[:-2])
    return line1, city, state, zip_code


def load_dealmachine(path: str, state: str | None = None,
                     limit: int | None = None) -> list[AddressInput]:
    """Read a DealMachine contacts export into AddressInput rows.

    We check the associated property address, not the mailing address, because
    the property is what gets fiber. state filters to one state, limit caps the
    count for a POC-sized sample.
    """
    addresses: list[AddressInput] = []
    seen: set[str] = set()
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            full = (row.get("associated_property_address_full")
                    or row.get("primary_mailing_address") or "").strip()
            if not full:
                continue
            line1, city, st, zip_code = parse_full_address(full)
            if not (line1 and st and zip_code):
                continue
            if state and st != state.upper():
                continue
            address = AddressInput(
                address_line1=line1, city=city, state=st, zip_code=zip_code,
                address_id=(row.get("contact_id") or "").strip(),
            )
            if address.key() in seen:
                continue  # one export can list several contacts per property
            seen.add(address.key())
            addresses.append(address)
            if limit and len(addresses) >= limit:
                break
    return addresses
