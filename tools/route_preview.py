"""Pre-scrape routing: decide which providers to check for each address.

Run this before any scraping. It uses each provider's coverage footprint to
route every address to only the providers that could serve it, so you never
spend a lookup (or a paid unblocker request) on a state a provider does not
operate in. No network calls happen here.

Example:
    python tools/route_preview.py addresses_mi_sample.csv --out routing.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

sys.path.insert(0, ".")

from run_poc import load_addresses
from serviceability.providers.att import AttChecker
from serviceability.providers.frontier import FrontierChecker
from serviceability.providers.kinetic import KineticChecker

PROVIDERS = [KineticChecker(), FrontierChecker(), AttChecker()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Route addresses to providers by footprint")
    parser.add_argument("addresses")
    parser.add_argument("--out", default="routing.csv")
    args = parser.parse_args()

    addresses = load_addresses(args.addresses)
    counts = {p.name: 0 for p in PROVIDERS}
    no_provider = 0

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["address_key", "address_line1", "city", "state", "zip", "providers_to_check"])
        for a in addresses:
            serving = [p.name for p in PROVIDERS if p.serves(a)]
            for name in serving:
                counts[name] += 1
            if not serving:
                no_provider += 1
            writer.writerow([a.key(), a.address_line1, a.city, a.state, a.zip_code,
                             ";".join(serving)])

    print(f"Routed {len(addresses)} addresses. Wrote {args.out}")
    for name, n in counts.items():
        print(f"  {name}: {n} addresses to check")
    if no_provider:
        print(f"  No provider in footprint: {no_provider} (skipped entirely)")


if __name__ == "__main__":
    main()
