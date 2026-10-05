"""Collect an inventory of Equinix Fabric ports and connections (incl. AWS/GCP
private links) using the Equinix Python SDK and dump it to JSON.

Auth: set EQUINIX_CLIENT_ID / EQUINIX_CLIENT_SECRET (OAuth2 client credentials
created in the portal; works for SSO-based accounts since API apps are issued
per user/org), or set EQUINIX_TOKEN to reuse an existing bearer token.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

from equinix.services import fabricv4

API = "https://api.equinix.com"
CLOUD_TYPES = {"AWS_DIRECT_CONNECT": "aws", "GOOGLE_CLOUD_INTERCONNECT": "gcp"}


def get_token():
    token = os.environ.get("EQUINIX_TOKEN")
    if token:
        return token
    data = json.dumps({
        "grant_type": "client_credentials",
        "client_id": os.environ["EQUINIX_CLIENT_ID"],
        "client_secret": os.environ["EQUINIX_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(
        urllib.parse.urljoin(API, "/oauth2/v1/token"), data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def to_dict(model):
    return model.to_dict() if hasattr(model, "to_dict") else model


def classify(conn):
    """Tag a connection as aws/gcp/other based on its A/Z side access point."""
    for side in ("a_side", "z_side"):
        ap = ((conn.get(side) or {}).get("access_point") or {})
        profile = ap.get("profile") or {}
        kind = CLOUD_TYPES.get(str(profile.get("type", "")))
        if kind:
            return kind
    return "other"


def main():
    client = fabricv4.ApiClient(fabricv4.Configuration(access_token=get_token()))

    ports = fabricv4.PortsApi(client).get_ports().data or []
    search = fabricv4.SearchRequest(
        filter=fabricv4.Expression(
            var_property="/uuid", operator="LIKE", values=["%"]),
        pagination=fabricv4.PaginationRequest(offset=0, limit=100))
    conns = fabricv4.ConnectionsApi(client).search_connections(search).data or []
    conns = [to_dict(c) for c in conns]
    for c in conns:
        c["cloud"] = classify(c)

    inventory = {"ports": [to_dict(p) for p in ports], "connections": conns}
    json.dump(inventory, sys.stdout, indent=2, default=str)
    print(f"\n# {len(ports)} ports, {len(conns)} connections", file=sys.stderr)


if __name__ == "__main__":
    main()
