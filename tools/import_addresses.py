"""Convert a client address export into the POC address CSV format.

Example:
    python tools/import_addresses.py "C:/path/dealmachine.csv" \
        --format dealmachine --state MI --limit 75 --out addresses_mi_sample.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

sys.path.insert(0, ".")

from serviceability.importers import load_dealmachine

COLUMNS = ["address_id", "address_line1", "unit", "city", "state", "zip", "note"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import client addresses into POC format")
    parser.add_argument("source")
    parser.add_argument("--format", choices=["dealmachine"], default="dealmachine")
    parser.add_argument("--state", default=None, help="keep only this state, e.g. MI")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default="addresses_imported.csv")
    args = parser.parse_args()

    addresses = load_dealmachine(args.source, state=args.state, limit=args.limit)
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for a in addresses:
            writer.writerow({
                "address_id": a.address_id, "address_line1": a.address_line1,
                "unit": a.unit, "city": a.city, "state": a.state, "zip": a.zip_code,
                "note": "",
            })
    print(f"Wrote {len(addresses)} unique addresses to {args.out}")


if __name__ == "__main__":
    main()
