# Equinix
Starter for collecting visibility into Equinix Fabric using the
[Equinix Python SDK](https://github.com/equinix/equinix-sdk-python) (`pip install equinix`).

## Quick start
```
pip install -r requirements.txt
export EQUINIX_CLIENT_ID=... EQUINIX_CLIENT_SECRET=...   # or EQUINIX_TOKEN=...
python collect_fabric_inventory.py > inventory.json
```
Outputs Fabric ports and connections; connections are tagged `aws`/`gcp`/`other`
(Direct Connect / Cloud Interconnect private links).

## Notes
- **SSO**: API calls can't use the SSO login. Create an application in the Equinix
  portal (Developer Platform → API Apps) to get client credentials, or reuse a bearer token.
- **Billing**: the SDK (v0.20) only ships `fabricv4`; there is no billing service module yet,
  so billing data isn't available via the SDK. Check the Equinix MCP server / portal
  invoices as a next step.
- Next steps: Cloud Routers (`CloudRoutersApi`), Service Profiles, metrics/stats per connection.
