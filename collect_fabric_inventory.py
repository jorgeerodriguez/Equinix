"""Collect an inventory of Equinix Fabric ports, connections (incl. AWS/GCP
private links), Cloud Routers and Network Edge devices, and dump it to JSON.

Auth: set EQUINIX_CLIENT_ID / EQUINIX_CLIENT_SECRET (OAuth2 client credentials
created in the portal; works for SSO-based accounts since API apps are issued
per user/org), or set EQUINIX_TOKEN to reuse an existing bearer token.

Progress/status messages go to stderr; the JSON inventory goes to stdout, e.g.
    python collect_fabric_inventory.py > inventory.json
Add --html to also write a shareable HTML report:
    python collect_fabric_inventory.py --html inventory.html > inventory.json
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from equinix.services import fabricv4

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


def collect(name, fetch):
    """Run one collection step, logging success/failure without aborting."""
    log(f"[{name}] Querying ...")
    try:
        items = fetch()
    except Exception as e:  # noqa: BLE001 - report and keep going
        log(f"[{name}] FAILED: {e}")
        return []
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


def get_network_edge_devices(token):
    # The Python SDK has no Network Edge module, so call the NE REST API directly.
    items, offset = [], 0
    while True:
        req = urllib.request.Request(
            f"{API}/ne/v1/devices?offset={offset}&limit={PAGE_SIZE}",
            headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, body = resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")
        page = body.get("data") or []
        total = (body.get("pagination") or {}).get("total", len(page))
        log(f"[network_edge] HTTP {status} - offset {offset}: "
            f"{len(page)} of {total}")
        items += page
        offset += PAGE_SIZE
        if not page or offset >= total:
            return items


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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", metavar="PATH",
                        help="also write an HTML report to PATH")
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
    inventory = {"ports": ports, "connections": conns,
                 "cloud_routers": routers, "network_edge_devices": devices}
    json.dump(inventory, sys.stdout, indent=2, default=str)
    print()

    print_tables(routers, conns, devices)
    log(f"\n# {len(ports)} ports | connections: "
        f"{len(conns['active'])} active, {len(conns['deprovisioned'])} "
        f"deprovisioned | cloud routers: {len(routers['active'])} active, "
        f"{len(routers['deprovisioned'])} deprovisioned | "
        f"{len(devices)} Network Edge devices")
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(inventory))
        log(f"HTML report written to {args.html}")


if __name__ == "__main__":
    main()
