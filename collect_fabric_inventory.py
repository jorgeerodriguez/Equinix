"""Collect an inventory of Equinix Fabric ports, connections (incl. AWS/GCP
private links), Cloud Routers and Network Edge devices, and dump it to JSON.

Auth: set EQUINIX_CLIENT_ID / EQUINIX_CLIENT_SECRET (OAuth2 client credentials
created in the portal; works for SSO-based accounts since API apps are issued
per user/org), or set EQUINIX_TOKEN to reuse an existing bearer token.

Progress/status messages go to stderr; the JSON inventory goes to stdout, e.g.
    python collect_fabric_inventory.py > inventory.json
Add --html to also write a shareable HTML report:
    python collect_fabric_inventory.py --html inventory.html > inventory.json

Besides inventory it collects bandwidth utilization (--days, default 90),
invoices from the Billing API, and list prices used for right-sizing.
"""
import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from equinix.services import fabricv4

from insights import (INVOICE_FIELDS, rightsizing, scrub, summarize_billing,
                      summarize_stats, trim_lines)
from report import render_html

load_dotenv()

API = os.environ.get("EQUINIX_URL", "https://api.equinix.com")
PAGE_SIZE = 100
# Fabric reports cloud service profiles as a generic L2_PROFILE, so match on the
# seller profile name instead of its type.
CLOUD_PROFILES = {"aws": ("aws",), "gcp": ("google",),
                  "azure": ("azure", "microsoft"), "oracle": ("oracle",)}
ROUTER_STATES = ["PROVISIONED", "PROVISIONING", "NOT_PROVISIONED",
                 "DEPROVISIONING", "DEPROVISIONED", "FAILED"]
# Anything in these states is reported as deprovisioned; everything else is
# considered active (including in-flight states like PROVISIONING).
INACTIVE_STATES = {"DEPROVISIONED", "DELETED", "NOT_PROVISIONED"}
PRICE_TIERS = [50, 100, 200, 500, 1000, 2000, 5000, 10000]


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def get_token():
    token = os.environ.get("EQUINIX_TOKEN")
    if token:
        log("[auth] Using EQUINIX_TOKEN from environment")
        return token
    data = json.dumps({
        "grant_type": "client_credentials",
        "client_id": os.environ["EQUINIX_CLIENT_ID"],
        "client_secret": os.environ["EQUINIX_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(
        urllib.parse.urljoin(API, "/oauth2/v1/token"), data=data,
        headers={"Content-Type": "application/json"})
    log(f"[auth] Requesting OAuth token from {API} ...")
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.load(resp)["access_token"]
    log(f"[auth] OK (HTTP {resp.status})")
    return token


def read_json(resp):
    """Decode a *_without_preload_content SDK response.

    The SDK's pydantic models reject some real-world payloads (e.g. connection
    change operations), so we read raw JSON instead of the typed models.
    """
    if resp.status >= 400:
        raise RuntimeError(f"HTTP {resp.status}: {resp.data.decode()[:300]}")
    return resp.status, json.loads(resp.data)


def collect(name, fetch, default=list):
    """Run one collection step, logging success/failure without aborting."""
    log(f"[{name}] Querying ...")
    try:
        items = fetch()
    except Exception as e:  # noqa: BLE001 - report and keep going
        log(f"[{name}] FAILED: {e}")
        return default()
    log(f"[{name}] OK - {len(items)} found")
    return items


def get_ports(client):
    status, body = read_json(
        fabricv4.PortsApi(client).get_ports_without_preload_content())
    log(f"[ports] HTTP {status}")
    return body.get("data") or []


def get_connections(client):
    api = fabricv4.ConnectionsApi(client)
    items, offset = [], 0
    while True:
        # /isRemote is either true or false, so this matches every connection.
        search = fabricv4.SearchRequest(
            filter=fabricv4.Expression(
                var_property="/isRemote", operator="=",
                values=["true", "false"]),
            pagination=fabricv4.PaginationRequest(
                offset=offset, limit=PAGE_SIZE))
        status, body = read_json(
            api.search_connections_without_preload_content(search))
        page = body.get("data") or []
        total = (body.get("pagination") or {}).get("total", len(page))
        log(f"[connections] HTTP {status} - offset {offset}: "
            f"{len(page)} of {total}")
        items += page
        offset += PAGE_SIZE
        if not page or offset >= total:
            return items


def get_cloud_routers(client):
    search = fabricv4.CloudRouterSearchRequest(
        filter=fabricv4.CloudRouterFilters(and_=[fabricv4.CloudRouterFilter(
            fabricv4.CloudRouterSimpleExpression(
                var_property="/state", operator="IN",
                values=ROUTER_STATES))]),
        pagination=fabricv4.PaginationRequest(offset=0, limit=PAGE_SIZE))
    status, body = read_json(fabricv4.CloudRoutersApi(client)
                             .search_cloud_routers_without_preload_content(search))
    log(f"[cloud_routers] HTTP {status}")
    return body.get("data") or []


def rest(token, path, method="GET", body=None, timeout=90):
    """Call an Equinix REST endpoint that the Python SDK doesn't cover."""
    req = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")


def paged(name, token, path):
    """Fetch every page of an offset/limit-paginated GET endpoint."""
    items, offset = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        status, body = rest(token, f"{path}{sep}offset={offset}&limit={PAGE_SIZE}")
        page = body.get("data") or []
        total = (body.get("pagination") or {}).get("total", len(page))
        log(f"[{name}] HTTP {status} - offset {offset}: {len(page)} of {total}")
        items += page
        offset += PAGE_SIZE
        if not page or offset >= total:
            return items


def get_network_edge_devices(token):
    # The Python SDK has no Network Edge module, so call the NE REST API directly.
    # scrub() drops the cluster node admin passwords the API returns.
    return [scrub(d) for d in paged("network_edge", token, "/ne/v1/devices")]


def get_utilization(token, conns, days):
    """Bandwidth utilization for each connection over the last `days` days."""
    end = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    start = end - datetime.timedelta(days=days)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    results = []
    for c in conns:
        try:
            status, body = rest(token, (
                f"/fabric/v4/connections/{c['uuid']}/stats"
                f"?startDateTime={start.strftime(fmt)}"
                f"&endDateTime={end.strftime(fmt)}&viewPoint=aSide"))
        except RuntimeError as e:
            # e.g. stats aren't offered for connections from virtual devices
            log(f"[utilization] skipped {c.get('name')}: {str(e)[:160]}")
            continue
        u = summarize_stats(c, body)
        log(f"[utilization] HTTP {status} - {c.get('name')}: peak "
            f"{u['peak']:.1f} Mbps, p95 {u['p95']:.1f} Mbps "
            f"({u['peak_pct']}% of {c.get('bandwidth')} Mbps at peak)")
        results.append(u)
    return results


def get_billing(token, account_number, names):
    """Invoices and invoice line items (last 12 months) for the account."""
    status, body = rest(token, f"/v2/invoices?accountNumber={account_number}")
    invoices = [{k: i.get(k) for k in INVOICE_FIELDS}
                for i in body.get("data") or []]
    log(f"[billing] HTTP {status} - {len(invoices)} invoices")
    lines = paged("billing", token,
                  f"/v2/invoices/details?accountNumber={account_number}")
    return invoices, trim_lines(lines, names)


def get_prices(token, metro):
    """List prices for Cloud Router -> cloud connections at each bandwidth."""
    status, body = rest(token, "/fabric/v4/prices/search", "POST", {
        "filter": {"and": [
            {"property": "/type", "operator": "=",
             "values": ["VIRTUAL_CONNECTION_PRODUCT"]},
            {"property": "/connection/type", "operator": "=", "values": ["IP_VC"]},
            {"property": "/connection/bandwidth", "operator": "IN",
             "values": PRICE_TIERS},
            {"property": "/connection/aSide/accessPoint/type", "operator": "=",
             "values": ["CLOUD_ROUTER"]},
            {"property": "/connection/zSide/accessPoint/type", "operator": "=",
             "values": ["SP"]},
            {"property": "/connection/aSide/accessPoint/location/metroCode",
             "operator": "=", "values": [metro]},
            {"property": "/connection/zSide/accessPoint/location/metroCode",
             "operator": "=", "values": [metro]}]}})
    log(f"[prices] HTTP {status}")
    return body.get("data") or []


def classify(conn):
    """Tag a connection as aws/gcp/azure/oracle/internet/other."""
    for side in ("aSide", "zSide"):
        profile = ((conn.get(side) or {}).get("accessPoint") or {}).get(
            "profile") or {}
        if profile.get("type") == "IA_PROFILE":
            return "internet"
        name = (profile.get("name") or "").lower()
        for kind, keywords in CLOUD_PROFILES.items():
            if any(k in name for k in keywords):
                return kind
    return "other"


def lifecycle(status):
    return "deprovisioned" if status in INACTIVE_STATES else "active"


def split_lifecycle(items, status):
    groups = {"active": [], "deprovisioned": []}
    for item in items:
        groups[lifecycle(status(item))].append(item)
    return groups


def conn_status(c):
    return (c.get("operation") or {}).get("equinixStatus")


def get(obj, path, default=""):
    """Fetch a nested value by '/'-separated path, e.g. 'aSide/accessPoint'."""
    for key in path.split("/"):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return default if obj is None else obj


def date(value):
    return str(value)[:10] if value else ""


def endpoint(side):
    """Short human-readable label for a connection's A/Z access point."""
    ap = get(side, "accessPoint", {})
    name = (get(ap, "router/name") or get(ap, "virtualDevice/name")
            or get(ap, "profile/name") or get(ap, "port/name")
            or get(ap, "network/name"))
    return f"{name} ({ap.get('type', '?')})" if name else ap.get("type", "")


def table(title, headers, rows):
    """Print a box-drawn table to stderr."""
    if not rows:
        log(f"\n{title}: none")
        return
    rows = [[str(v) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(headers)]

    def line(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in widths) + right

    def fmt(cells):
        return "│" + "│".join(f" {c:<{w}} " for c, w in zip(cells, widths)) + "│"

    log(f"\n{title} ({len(rows)})")
    log(line("┌", "┬", "┐"))
    log(fmt(headers))
    log(line("├", "┼", "┤"))
    for row in rows:
        log(fmt(row))
    log(line("└", "┴", "┘"))


def print_tables(routers, conns, devices):
    for state in ("active", "deprovisioned"):
        last_col = "Deleted" if state == "deprovisioned" else "Updated"
        last_path = ("changeLog/deletedDateTime" if state == "deprovisioned"
                     else "changeLog/updatedDateTime")
        table(f"Cloud Routers - {state}",
              ["Name", "State", "Package", "Metro", "ASN", "Conns",
               "Created", last_col],
              [[r.get("name"), r.get("state"), get(r, "package/code"),
                get(r, "location/metroCode"), r.get("equinixAsn", ""),
                r.get("connectionsCount", ""),
                date(get(r, "changeLog/createdDateTime")),
                date(get(r, last_path))] for r in routers[state]])
        table(f"Connections - {state}",
              ["Name", "Status", "Type", "Cloud", "Mbps", "A-side", "Z-side",
               "Metro", "Created", last_col],
              [[c.get("name"), conn_status(c), c.get("type"), c.get("cloud"),
                c.get("bandwidth", ""), endpoint(c.get("aSide")),
                endpoint(c.get("zSide")),
                get(c, "aSide/accessPoint/location/metroCode"),
                date(get(c, "changeLog/createdDateTime")),
                date(get(c, last_path))] for c in conns[state]])
    table("Network Edge devices",
          ["Name", "Status", "Type", "Vendor", "Metro", "IBX", "Created"],
          [[d.get("name"), d.get("status"), d.get("deviceTypeCode"),
            d.get("deviceTypeVendor"), d.get("metroCode"), d.get("ibx"),
            date(d.get("createdDate"))] for d in devices])


def money(value):
    return f"${value:,.0f}"


def print_insights(util, billing, sizing):
    table("Utilization (active connections)",
          ["Name", "Cloud", "Capacity", "Peak Mbps", "p95 Mbps", "Peak %"],
          [[u["name"], u["cloud"], f"{u['bandwidth']} Mbps", f"{u['peak']:.1f}",
            f"{u['p95']:.1f}", f"{u['peak_pct']}%"] for u in util])
    if billing:
        table("Monthly spend (invoice month)",
              ["Month", "Charges", "Credits"],
              [[m["month"], money(m["total_charges"]), money(m["credits"])]
               for m in billing["monthly"]])
        table("Current month by item", ["Item", "Category", "Amount"],
              [[i["item"], i["category"], money(i["amount"])]
               for i in billing["current_items"]])
    if sizing and sizing["links"]:
        table(f"Right-sizing (pair peak x {sizing['headroom']} headroom)",
              ["Link", "Current", "Pair peak", "Action", "Recommended",
               "Savings/mo"],
              [[r["name"], f"{r['bandwidth']} Mbps", f"{r['combined_peak']} Mbps",
                r["action"], f"{r['recommended']} Mbps" if r["recommended"]
                else "-", money(r["savings"])]
               for r in sizing["links"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", metavar="PATH",
                        help="also write an HTML report to PATH")
    parser.add_argument("--days", type=int, default=90,
                        help="utilization window in days (default 90)")
    args = parser.parse_args()

    token = get_token()
    client = fabricv4.ApiClient(fabricv4.Configuration(access_token=token))
    log("Collecting Fabric / Network Edge inventory ...")

    ports = collect("ports", lambda: get_ports(client))
    conns = collect("connections", lambda: get_connections(client))
    for c in conns:
        c["cloud"] = classify(c)
    routers = collect("cloud_routers", lambda: get_cloud_routers(client))
    devices = collect("network_edge", lambda: get_network_edge_devices(token))

    conns = split_lifecycle(conns, conn_status)
    routers = split_lifecycle(routers, lambda r: r.get("state"))

    util = collect("utilization",
                   lambda: get_utilization(token, conns["active"], args.days))
    names = {x["uuid"]: x.get("name") for group in (conns, routers)
             for state in group.values() for x in state}
    for d in devices:
        names[d["uuid"]] = d.get("name")
        for node in get(d, "clusterDetails/nodes", []):
            names[node.get("uuid")] = node.get("name")
    account = next((get(c, "account/accountNumber") for state in conns.values()
                    for c in state), None)
    billing = None
    if account:
        invoices, lines = collect("billing", lambda: get_billing(
            token, account, names), default=lambda: ([], []))
        if invoices:
            billing = summarize_billing(invoices, lines)
            billing["invoices"] = invoices
    metro = next((get(c, "aSide/accessPoint/location/metroCode")
                  for c in conns["active"]), "DC")
    prices = collect("prices", lambda: get_prices(token, metro))
    sizing = rightsizing(util, prices, billing["current_items"] if billing else [])

    inventory = {"ports": ports, "connections": conns,
                 "cloud_routers": routers, "network_edge_devices": devices,
                 "utilization": {"days": args.days, "connections": util},
                 "billing": billing, "rightsizing": sizing}
    json.dump(inventory, sys.stdout, indent=2, default=str)
    print()

    print_tables(routers, conns, devices)
    print_insights(util, billing, sizing)
    log(f"\n# {len(ports)} ports | connections: "
        f"{len(conns['active'])} active, {len(conns['deprovisioned'])} "
        f"deprovisioned | cloud routers: {len(routers['active'])} active, "
        f"{len(routers['deprovisioned'])} deprovisioned | "
        f"{len(devices)} Network Edge devices")
    if billing:
        log(f"# Run-rate {money(billing['run_rate'])}/mo | last 12 months "
            f"{money(billing['last12_total'])} | potential savings "
            f"{money(sizing['monthly_savings'])}/mo")
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(inventory))
        log(f"HTML report written to {args.html}")


if __name__ == "__main__":
    main()
