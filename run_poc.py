"""POC entry point.

Loads addresses, runs the selected provider checkers, stores every result with a
date stamp, writes the unified results CSV, then runs the comparison engine to
surface fresh fiber leads (addresses that flipped Not Available to Available) and
writes those to their own CSV.

Examples:
    python run_poc.py --mock                       offline pipeline demo
    python run_poc.py --provider kinetic           live Kinetic run
    python run_poc.py --provider all --no-headless live run, visible browser
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timezone

from serviceability.compare import find_fresh_leads
from serviceability.config import load_config
from serviceability.models import AddressInput, CheckResult
from serviceability.output.csv_writer import write_fresh_leads, write_results
from serviceability.storage.db import ResultStore


def load_addresses(path: str) -> list[AddressInput]:
    addresses: list[AddressInput] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            addresses.append(
                AddressInput(
                    address_line1=row.get("address_line1", "").strip(),
                    unit=row.get("unit", "").strip(),
                    city=row.get("city", "").strip(),
                    state=row.get("state", "").strip(),
                    zip_code=row.get("zip", "").strip(),
                    address_id=row.get("address_id", "").strip(),
                )
            )
    return addresses


def build_checkers(provider: str, mock: bool, config, addresses):
    if mock:
        from serviceability.providers.mock import MockChecker
        # First run: nothing available. Re-run with MOCK_AVAILABLE to flip some.
        available = _mock_available_keys(addresses)
        checkers = []
        if provider in ("att", "all"):
            checkers.append(MockChecker("AT&T", available))
        if provider in ("kinetic", "all"):
            checkers.append(MockChecker("Kinetic", available))
        if provider in ("frontier", "all"):
            checkers.append(MockChecker("Frontier", available))
        return checkers

    from serviceability.providers.att import AttChecker
    from serviceability.providers.frontier import FrontierChecker
    from serviceability.providers.kinetic import KineticChecker
    checkers = []
    if provider in ("kinetic", "all"):
        checkers.append(KineticChecker(headless=config.headless, proxy=config.next_proxy(),
                                       pacing=config.pacing))
    if provider in ("frontier", "all"):
        checkers.append(FrontierChecker(headless=config.headless, proxy=config.next_proxy(),
                                        pacing=config.pacing))
    if provider in ("att", "all"):
        checkers.append(AttChecker(headless=config.headless, proxy=config.next_proxy(),
                                   pacing=config.pacing, unblocker=config.unblocker))
    return checkers


def _mock_available_keys(addresses) -> set[str]:
    flag = os.environ.get("MOCK_AVAILABLE", "")
    if flag == "all":
        return {a.key() for a in addresses}
    if flag == "half":
        return {a.key() for i, a in enumerate(addresses) if i % 2 == 0}
    return set()


def run(args) -> None:
    config = load_config(args.config)
    if args.no_headless:
        config.headless = False
    addresses = load_addresses(args.addresses)
    print(f"Loaded {len(addresses)} addresses from {args.addresses}")

    store = ResultStore(config.db_path)
    os.makedirs(args.out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    all_results: list[CheckResult] = []
    for checker in build_checkers(args.provider, args.mock, config, addresses):
        served = [a for a in addresses if checker.serves(a)]
        skipped = len(addresses) - len(served)
        print(f"Routing {checker.name}: {len(served)} in footprint, "
              f"{skipped} skipped as out of footprint")
        if not served:
            checker.close()
            continue
        try:
            results = checker.check_many(served)
        finally:
            checker.close()
        all_results.extend(results)
        _print_summary(checker.name, results)

    store.save(all_results)
    results_path = os.path.join(args.out_dir, f"results_{stamp}.csv")
    write_results(results_path, all_results)
    print(f"Wrote {len(all_results)} results to {results_path}")

    leads = find_fresh_leads(store, all_results)
    leads_path = os.path.join(args.out_dir, f"fresh_leads_{stamp}.csv")
    write_fresh_leads(leads_path, leads)
    print(f"Found {len(leads)} fresh fiber lead(s). Wrote {leads_path}")
    for lead in leads:
        print(f"  FRESH: {lead.address_key} [{lead.provider}] "
              f"{lead.previous_category} -> {lead.current_category}")


def _print_summary(provider: str, results: list[CheckResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.category.value] = counts.get(r.category.value, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    print(f"  {provider} -> {summary}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ISP fiber serviceability POC")
    parser.add_argument("--provider", choices=["att", "kinetic", "frontier", "all"], default="all")
    parser.add_argument("--addresses", default="addresses_seed.csv")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out-dir", default="output")
    parser.add_argument("--mock", action="store_true",
                        help="run offline mock checkers to test the pipeline")
    parser.add_argument("--no-headless", action="store_true",
                        help="show the browser window during a live run")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
