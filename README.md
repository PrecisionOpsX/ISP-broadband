# ISP Fiber Serviceability Checker (POC)

Checks broadband fiber serviceability for residential addresses, stores every
result with a date stamp, and flags addresses that flip from Not Available to
Available. Those flips are the product: fresh fiber leads.

Providers in this POC: Kinetic (Windstream), Frontier, and AT&T. It is built as
the foundation of the full system, so storage, comparison, routing, and output
are provider-agnostic and each provider is isolated behind one interface.

## Provider status

| Provider | Status | Notes |
| --- | --- | --- |
| Kinetic | Live, decoded | Reads the real qualification API (buy.gokinetic.com/api/v1/address/search). Lightly defended; needs a clean residential IP |
| Frontier | Live, decoded | Drives frontier.com, reads /ol/api/v2/serviceability. Confirm the final verdict field once on a clean IP with confirm_endpoint |
| AT&T | Set up, needs unblocker | Behind Akamai Bot Manager; free automation gets 403. Routes through a commercial unblocker once a key is set in config.yaml. Deprioritized per client |

IMPORTANT: run real checks from a clean residential IP. A VPN or datacenter IP
(for example Astrill) is flagged by the providers and gets blocked or soft
blocked. The code is correct; the exit IP is what matters.

## Setup (on the clean machine)

```
pip install -r requirements.txt
python -m playwright install chrome
```

`python -m playwright install chrome` installs the real Chrome the checkers drive.
If that is unavailable, `python -m playwright install chromium` also works.

## Step 1: import the client's address file

The client sends DealMachine exports. Convert one to the POC format, optionally
filtering by state and sampling a few hundred for the POC.

```
python tools/import_addresses.py "C:/path/to/dealmachine.csv" --state KY --limit 150 --out addresses_ky.csv
```

## Step 2: route before scraping (free, no network)

See which providers each address should be checked against, by footprint, so you
never waste a lookup (or a paid AT&T unblocker request) on a provider that does
not operate there.

```
python tools/route_preview.py addresses_ky.csv --out routing.csv
```

## Step 3: run the checks

```
cp config.example.yaml config.yaml          then set proxies and (later) the AT&T unblocker
python run_poc.py --provider kinetic  --addresses addresses_ky.csv
python run_poc.py --provider frontier --addresses addresses_mi.csv
python run_poc.py --provider all      --addresses addresses.csv --no-headless
```

`--no-headless` shows the browser so you can watch it work. Running the same
command again on a later date automatically compares against history and surfaces
new fiber.

## Step 4: the output, and what to deliver to the client

Each run writes two timestamped files to `output/`:

- `results_<timestamp>.csv` one row per address per provider, with the category
  (Fiber Available, Not Available, Existing Customer, Unable to Verify), the
  technology, speed, matched address, and the check date. This is the full
  dataset.
- `fresh_leads_<timestamp>.csv` only the addresses that flipped Not Available to
  Available since the last run. This is empty on the very first run because there
  is no history yet to compare against.

Deliver BOTH to the client:
- On the first run, send `results_<timestamp>.csv` (the serviceability snapshot).
- On every later run, send `fresh_leads_<timestamp>.csv` (the new fiber, their
  actual product) plus the latest `results_<timestamp>.csv` for the full picture.

## Test the pipeline offline (no network)

Proves storage, comparison, and CSV output, and demonstrates flip detection.

```
python run_poc.py --mock --provider all
MOCK_AVAILABLE=half python run_poc.py --mock --provider all
python -m unittest discover -s tests
```

## The six categories

| Category | Meaning | Set by |
| --- | --- | --- |
| Fiber Available | Provider sells fiber at this address now | live check |
| Not Available | Address recognized, no fiber | live check |
| Existing Customer | Provider already serves this address | live check |
| Unable to Verify | Could not get a clean answer this run | live check |
| On FCC but not live | In FCC Fabric, no live fiber | FCC cross-ref (full build) |
| Live on provider site but not on FCC | Live fiber, not yet on FCC map | FCC cross-ref (full build) |

The first four come from the live checkers. The last two need the FCC Broadband
Fabric and are produced by the cross-reference layer in the full build. Fresh
buildout does not appear on the FCC map for up to six months, so a live flip is
the source of truth for what is new.

## AT&T unblocker

AT&T is behind Akamai. To get reliable results, set an unblocker in config.yaml:

```
unblocker:
  enabled: true
  provider: brightdata
  proxy: http://brd-customer-XXXX-zone-YYYY:PASSWORD@brd.superproxy.io:22225
```

The AT&T checker then routes its browser through that unblocker. Bright Data Web
Unlocker is recommended for Akamai; Oxylabs Web Unblocker works the same way.

## Coverage routing is coarse for now

Routing is state-level. It correctly rules out clearly out-of-footprint providers
(for example Kinetic does not serve Michigan), but within a state different areas
have different providers, so it cannot tell that one Kentucky town is Kinetic and
another is AT&T. The full build refines this to zip and census-block level using
FCC Fabric data. Until then, target runs with --provider.

## Scaling to the full system

- Add a provider: write one class against ProviderChecker. Nothing else changes.
- Add workers and proxies: checkers are independent and proxy-aware already.
- Swap SQLite for Postgres: the store is the only file that changes.
- Add Quantum Fiber and the FCC cross-reference layer.
