# Equinix Interconnect Report

Collects inventory, spend, utilization, BGP reliability and routing data for an
Equinix Fabric and Network Edge account. Writes it out as JSON, an interactive
HTML report and a PDF for leadership.

It uses the [Equinix Python SDK](https://github.com/equinix/equinix-sdk-python)
(`fabricv4`) where it can. For the APIs the SDK doesn't cover (Network Edge,
Billing, connection statistics) it calls the Equinix REST API directly with the
same credentials.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (it is git-ignored):

```
EQUINIX_CLIENT_ID=...
EQUINIX_CLIENT_SECRET=...
# or, instead of client credentials:
# EQUINIX_TOKEN=...
```

API calls can't use an SSO login. Create an application in the Equinix portal
(Developer Platform → API Apps) to get client credentials.

The PDF needs Google Chrome, Chromium or Microsoft Edge. If yours isn't in a
standard location, set `CHROME_PATH` to its executable.

## Usage

```bash
# JSON to stdout, progress and summary tables to stderr
python collect_fabric_inventory.py > inventory.json

# Also write the HTML report and a PDF (reports/Equinix_Report_{Date}_{Time}.pdf)
python collect_fabric_inventory.py --html report.html --pdf > inventory.json
```

| Option | Default | What it does |
|---|---|---|
| `--html PATH` | off | Write the interactive HTML report to `PATH` |
| `--pdf [DIR]` | off (`reports/` if no folder given) | Write `Equinix_Report_YYYY-MM-DD_HHMM.pdf` to `DIR` |
| `--days N` | 90 | Utilization window in days. BGP events are capped at 89 days, which is all Equinix keeps. |

A full run takes about 40 seconds. Every call is read-only, and if one API
fails the rest still run; the failure is reported in the log.

## What the report contains

| Section | Contents | Source |
|---|---|---|
| Summary | Run-rate, 12-month spend, potential savings, peak traffic, lowest link availability, footprint, prioritized findings | All of the below |
| Spend | Monthly charges by category (12 months) and the latest invoice by item | Billing v2 `/v2/invoices`, `/v2/invoices/details` |
| Utilization | Average, 95th percentile and peak per link; 90-day hourly charts | Fabric `/connections/{id}/stats` |
| Right-sizing | Per-link action (keep, downsize, decommission), cost now and after | Utilization + Fabric `/prices/search` + invoices |
| Reliability | BGP outages, flaps, downtime and availability per link; session timeline; BFD status | Fabric Cloud Events, routing protocols |
| Routing | Networks learned by the Cloud Router and whether each has a backup path | Fabric `/routers/{id}/routes/search` |
| Inventory | Network Edge devices, Cloud Routers, connections (active and deprovisioned) | Fabric v4 (SDK), Network Edge `/ne/v1/devices` |

How the numbers are worked out:

- **Invoice lines** are matched to connections through the connection ID in
  the line description. Network Edge and Cloud Router lines are matched by
  their own IDs.
- **Right-sizing** treats links with the same cloud and bandwidth as a
  redundant pair. Each link must carry the pair's combined peak on its own,
  plus 50% headroom. A pair whose combined peak stays under 1 Mbps is flagged
  for decommissioning, and the report lists any networks it still carries.
- **Outages vs flaps:** a BGP session drop of 60 seconds or longer is an
  outage; a shorter one is a flap. When three or more sessions re-establish
  within 5 minutes with no drop recorded, it is reported as a network-wide
  reset.

These thresholds are constants at the top of `insights.py`.

## Files

| File | Purpose |
|---|---|
| `collect_fabric_inventory.py` | Entry point: API calls, CLI options, terminal tables |
| `insights.py` | Pure analysis: utilization stats, spend, right-sizing, BGP health, routes, secret scrubbing |
| `report.py` | Builds the self-contained HTML report (inline SVG charts, no external JS) |
| `pdf_export.py` | Prints the HTML report to PDF with headless Chrome |

## Security

The Equinix APIs return several secrets in plain text: FortiGate admin
passwords (Network Edge), BGP authentication keys (routing protocols), and
Google pairing keys and AWS account IDs (connection `authenticationKey`).
`insights.scrub()` removes these before anything is written.

The outputs still contain billing amounts, account names, IP ranges and ASNs.
Treat `inventory.json`, the HTML report and the PDFs as internal documents.
`reports/` is git-ignored for that reason.

## Limitations

- The SDK (v0.20) only includes `fabricv4`, and its typed models reject some
  real responses, so the script reads raw JSON from the SDK.
- Equinix doesn't provide utilization or BGP data for connections that start
  at a Network Edge device.
- Peaks are 5-minute averages, so shorter bursts can be higher.
- Prices are Equinix list prices for the metro; they matched the invoiced
  amounts for this account.
- FortiGate CPU and memory aren't available through the Equinix APIs.
