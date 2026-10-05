"""Render the Equinix inventory, spend and utilization as a self-contained HTML
report aimed at leadership: summary first, detail further down."""
import datetime
import json
from collections import defaultdict

from insights import CATEGORIES

CLOUD_NAMES = {"aws": "AWS", "gcp": "Google Cloud", "azure": "Azure",
               "oracle": "Oracle", "internet": "Internet", "other": "Other"}
# Cloud-side attachment sizes; a link plateauing just under one of these
# (below its own Equinix bandwidth) suggests the cloud side is the bottleneck.
CLOUD_SIDE_SIZES = [1000, 2000, 5000]


def get(obj, path, default=""):
    """Fetch a nested value by '/'-separated path, e.g. 'aSide/accessPoint'."""
    for key in path.split("/"):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return default if obj is None else obj


def money(value):
    return f"${value:,.0f}"


def bandwidth(mbps):
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps:g} Mbps"


def month_name(month):
    return datetime.date.fromisoformat(month + "-01").strftime("%b %Y")


# ------------------------------------------------------------------ inventory

def endpoint(side):
    ap = get(side, "accessPoint", {})
    name = (get(ap, "router/name") or get(ap, "virtualDevice/name")
            or get(ap, "profile/name") or get(ap, "port/name")
            or get(ap, "network/name"))
    return {"name": name or ap.get("type", ""), "kind": ap.get("type", "")}


def connection_row(c, lifecycle):
    return {
        "lifecycle": lifecycle,
        "uuid": c.get("uuid"),
        "name": c.get("name"),
        "status": get(c, "operation/equinixStatus"),
        "type": c.get("type"),
        "cloud": c.get("cloud"),
        "mbps": c.get("bandwidth") or 0,
        "priority": get(c, "redundancy/priority"),
        "a": endpoint(c.get("aSide")),
        "z": endpoint(c.get("zSide")),
        "metro": get(c, "aSide/accessPoint/location/metroCode"),
        "created": get(c, "changeLog/createdDateTime")[:10],
        "updated": get(c, "changeLog/updatedDateTime")[:10],
        "deleted": get(c, "changeLog/deletedDateTime")[:10],
    }


def router_row(r, lifecycle):
    return {
        "lifecycle": lifecycle,
        "name": r.get("name"),
        "state": r.get("state"),
        "package": get(r, "package/code"),
        "metro": get(r, "location/metroCode"),
        "metroName": get(r, "location/metroName"),
        "asn": r.get("equinixAsn", ""),
        "conns": r.get("connectionsCount", 0),
        "created": get(r, "changeLog/createdDateTime")[:10],
        "updated": get(r, "changeLog/updatedDateTime")[:10],
        "deleted": get(r, "changeLog/deletedDateTime")[:10],
    }


def device_row(d):
    nodes = get(d, "clusterDetails/nodes", [])
    return {
        "name": get(d, "clusterDetails/clusterName") or d.get("name"),
        "nodes": ", ".join(n.get("name", "") for n in nodes) or d.get("name"),
        "status": d.get("status"),
        "type": d.get("deviceTypeName") or d.get("deviceTypeCode"),
        "vendor": d.get("deviceTypeVendor"),
        "metro": d.get("metroCode"),
        "metroName": d.get("metroName"),
        "ibx": d.get("ibx"),
        "created": str(d.get("createdDate") or "")[:10],
    }


# ---------------------------------------------------------------- utilization

def utilization_rows(util, costs):
    """Per-link summary plus hourly series aligned to one shared time axis."""
    links = [u for u in util if u["series"]["hours"]]
    if not links:
        return None
    parse = lambda h: datetime.datetime.fromisoformat(h.replace("Z", "+00:00"))
    start = min(parse(u["series"]["hours"][0]) for u in links)
    end = max(parse(u["series"]["hours"][-1]) for u in links)
    n = int((end - start).total_seconds() // 3600) + 1
    rows = []
    for u in links:
        inbound, outbound = [None] * n, [None] * n
        for h, i, o in zip(u["series"]["hours"], u["series"]["in"],
                           u["series"]["out"]):
            idx = int((parse(h) - start).total_seconds() // 3600)
            inbound[idx], outbound[idx] = round(i, 1), round(o, 1)
        rows.append({
            "uuid": u["uuid"], "name": u["name"], "cloud": u["cloud"],
            "bandwidth": u["bandwidth"], "priority": u.get("priority"),
            "peak": round(u["peak"], 1), "p95": round(u["p95"], 1),
            "mean": round(max(u["inbound"]["mean"], u["outbound"]["mean"]), 2),
            "peak_pct": u["peak_pct"], "cost": costs.get(u["uuid"]),
            "in": inbound, "out": outbound,
        })
    order = {"aws": 0, "gcp": 1}
    rows.sort(key=lambda r: (order.get(r["cloud"], 9), -r["bandwidth"],
                             r["priority"] != "PRIMARY"))
    return {"start": start.isoformat().replace("+00:00", "Z"), "hours": n,
            "links": rows}


def peak_days(util_rows, top=3):
    """Dates of the biggest bursts that hit AWS and Google links at once (at
    least half the largest burst on each side)."""
    if not util_rows:
        return []
    start = datetime.datetime.fromisoformat(util_rows["start"].replace("Z", "+00:00"))
    per_cloud = cloud_totals(util_rows)
    if len(per_cloud) < 2:
        return []
    clouds = list(per_cloud.values())
    peaks = [max(c) for c in clouds]
    days = {}
    for idx in sorted(range(util_rows["hours"]), key=lambda i: -sum(c[i] for c in clouds)):
        if any(c[idx] < 0.5 * p for c, p in zip(clouds, peaks)):
            continue
        when = start + datetime.timedelta(hours=idx)
        days.setdefault(when.date(), when.hour)
        if len(days) == top:
            break
    return [(d.strftime("%b %-d"), h) for d, h in sorted(days.items())]


def cloud_totals(util_rows):
    """Hourly combined traffic per cloud (busier direction on each link)."""
    totals = {}
    for link in util_rows["links"]:
        if link["cloud"] not in ("aws", "gcp"):
            continue
        series = totals.setdefault(link["cloud"], [0.0] * util_rows["hours"])
        for idx, (i, o) in enumerate(zip(link["in"], link["out"])):
            series[idx] += max(i or 0, o or 0)
    return totals


# ------------------------------------------------------------------ findings

def duration(seconds):
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def short_date(iso):
    return datetime.datetime.fromisoformat(iso).strftime("%b %-d")


def reliability_findings(health, routes, sizing):
    out = []
    if health:
        links = [h for h in health["links"] if h["monitored"]]
        idle = {r["uuid"] for r in (sizing or {}).get("links", [])
                if r["action"] == "decommission"}
        by_uuid = {h["uuid"]: h for h in links}
        prod = [h for h in links if h["uuid"] not in idle and h["outages"]]
        if prod:
            worst = max(prod, key=lambda h: h["downtime_seconds"])
            first = worst["outages"][0]
            sized = {r["uuid"]: r for r in (sizing or {}).get("links", [])}
            mine = sized.get(worst["uuid"], {})
            partner = next((r for r in sized.values() if r["uuid"] != worst["uuid"]
                            and (r["cloud"], r["bandwidth"]) ==
                            (mine.get("cloud"), mine.get("bandwidth"))), None)
            partner_ok = partner and not any(
                o["start"] < first["end"] and o["end"] > first["start"]
                for o in by_uuid.get(partner["uuid"], {}).get("outages", []))
            out.append(("warn", f"{len(prod)} production link{'s' if len(prod) > 1 else ''} "
                        f"had outages in the last {health['days']} days",
                        f"Longest: {worst['name']}, {duration(worst['downtime_seconds'])} "
                        f"in total starting {short_date(first['start'])}"
                        + (f"; its partner {partner['name']} stayed up, so traffic "
                           "kept flowing." if partner_ok else ".")))
        flappy = [h for h in links if len(h["flaps"]) >= 5]
        if flappy:
            n = sum(len(h["flaps"]) for h in flappy)
            longest = max(f["seconds"] for h in flappy for f in h["flaps"])
            clouds = sorted({CLOUD_NAMES.get(h["cloud"], h["cloud"]) for h in flappy})
            out.append(("warn", f"{' and '.join(clouds)} BGP sessions drop often",
                        f"{n} brief drops across {len(flappy)} links, each back within "
                        f"{longest} s. The redundant pair absorbs them, but it is worth "
                        "raising with the provider."))
        no_bfd = [h for h in links if h.get("bfd") is False]
        if no_bfd and len(no_bfd) == len(links):
            out.append(("info", "BFD is off on every link",
                        "Without BFD, failover waits for the BGP hold timer (often "
                        "90 seconds or more). Enabling BFD on both sides cuts that to "
                        "about a second."))
    single = [r for r in routes or [] if not r["redundant"]]
    if single:
        out.append(("warn", f"{len(single)} network{'s' if len(single) > 1 else ''} "
                    "with a single path",
                    ", ".join(f"{r['prefix']} (via {r['paths'][0]['connection']})"
                              for r in single) + " has no backup connection on the "
                    "Cloud Router."))
    return out


def findings(billing, util_rows, sizing, active_conns):
    """Plain-language key findings, most important first."""
    out = []
    if billing and billing["monthly"]:
        first, last = billing["monthly"][0], billing["monthly"][-1]
        change = last["total_charges"] - first["total_charges"]
        if first["total_charges"] and abs(change) / first["total_charges"] >= 0.05:
            pct = abs(change) / first["total_charges"] * 100
            out.append(("good" if change < 0 else "warn",
                        f"Monthly spend is {'down' if change < 0 else 'up'} {pct:.0f}%",
                        f"{money(last['total_charges'])} in {month_name(last['month'])}, "
                        f"compared with {money(first['total_charges'])} in "
                        f"{month_name(first['month'])}."))
        cats = last["charges"]
        top_cat = max(cats, key=cats.get)
        if last["total_charges"]:
            share = cats[top_cat] / last["total_charges"] * 100
            out.append(("info", f"{top_cat} is {share:.0f}% of monthly spend",
                        f"{money(cats[top_cat])} of {money(last['total_charges'])} "
                        f"on the {month_name(last['month'])} invoice."))
    if sizing and sizing["monthly_savings"] > 0:
        idle = [r for r in sizing["links"] if r["action"] == "decommission"]
        down = [r for r in sizing["links"] if r["action"] == "downsize"]
        parts = []
        if idle:
            carried = sorted({p for r in idle for p in r.get("prefixes", [])})
            parts.append(f"decommissioning {len(idle)} idle links "
                         f"({', '.join(r['name'] for r in idle)}"
                         + (f", after moving or retiring {', '.join(carried)}"
                            if carried else "") + ")")
        if down:
            tiers = sorted({(r["bandwidth"], r["recommended"]) for r in down})
            parts.append(f"downsizing {len(down)} links "
                         f"({'; '.join(f'{bandwidth(a)} to {bandwidth(b)}' for a, b in tiers)})")
        out.append(("good", f"Save about {money(sizing['monthly_savings'])} per month "
                    f"({money(sizing['monthly_savings'] * 12)} per year)",
                    "By " + " and ".join(parts) + ", while keeping enough capacity "
                    "for one link to carry its pair's peak traffic."))
    if util_rows:
        links = util_rows["links"]
        busiest = max(links, key=lambda r: r["peak_pct"])
        out.append(("info", "Cloud links run far below capacity",
                    f"The busiest link, {busiest['name']}, peaked at "
                    f"{busiest['peak_pct']:.1f}% of its {bandwidth(busiest['bandwidth'])}. "
                    f"Typical (95th percentile) load is at most "
                    f"{max(r['p95'] for r in links):.1f} Mbps on any link."))
        days = peak_days(util_rows)
        if days:
            out.append(("info", "Traffic comes in short bursts",
                        "The largest bursts were on " + " and ".join(
                            f"{d} ({h:02d}:00 UTC)" for d, h in days) +
                        ", on AWS and Google links at the same time, which "
                        "points to scheduled cloud-to-cloud transfers."))
        capped = [(r, size) for r in links for size in CLOUD_SIDE_SIZES
                  if size < r["bandwidth"] and 0.9 * size <= r["peak"] <= size]
        if len(capped) >= 2:
            size = bandwidth(min(s for _, s in capped))
            out.append(("warn", "Links may be capped on the cloud side",
                        f"{len(capped)} links top out just under {size} although they "
                        "are provisioned at more than that on Equinix. Check whether the "
                        f"AWS hosted connections or Google VLAN attachments are {size}; "
                        "that would limit transfers whatever the Equinix tier."))
    measured = {r["uuid"] for r in (util_rows or {}).get("links", [])}
    skipped = [c for c in active_conns if c["a"]["kind"] == "VD"
               and c.get("uuid") not in measured]
    if skipped:
        out.append(("info", "Some links have no utilization data",
                    f"Equinix doesn't report statistics for connections that start "
                    f"at a Network Edge device ({', '.join(c['name'] for c in skipped)})."))
    return [{"tone": t, "title": a, "body": b} for t, a, b in out]


def order_findings(items):
    """Warnings first, then savings/good news, then context."""
    rank = {"warn": 0, "good": 1, "info": 2}
    return sorted(items, key=lambda f: rank.get(f["tone"], 3))


# ---------------------------------------------------------------------- build

def build_data(inventory):
    conns, routers = inventory["connections"], inventory["cloud_routers"]
    billing = inventory.get("billing") or None
    sizing = inventory.get("rightsizing") or None
    health = inventory.get("reliability") or None
    routes = inventory.get("routes") or []
    if sizing:
        # Networks each link carries, so decommissioning can be checked.
        for link in sizing["links"]:
            link["prefixes"] = [r["prefix"] for r in routes
                                if any(p["connection"] == link["name"] for p in r["paths"])]
    util = (inventory.get("utilization") or {}).get("connections") or []
    costs = {i["uuid"]: i["amount"] for i in (billing or {}).get("current_items", [])
             if i.get("uuid")}
    util_rows = utilization_rows(util, costs)
    conn_rows = [connection_row(c, s) for s in ("active", "deprovisioned")
                 for c in conns[s]]
    active = [r for r in conn_rows if r["lifecycle"] == "active"]
    cloud_links = [r for r in active if r["cloud"] in ("aws", "gcp")]
    devices = [device_row(d) for d in inventory["network_edge_devices"]]
    node_count = sum(len(get(d, "clusterDetails/nodes", [])) or 1
                     for d in inventory["network_edge_devices"])
    # Busiest cloud's combined peak; AWS and Google aren't added together
    # because cloud-to-cloud traffic crosses both and would count twice.
    peak_total = max((max(series) for series in
                      cloud_totals(util_rows).values()), default=0) if util_rows else 0
    return {
        "generated": datetime.datetime.now().strftime("%b %-d, %Y %H:%M"),
        "account": next((get(r, "account/accountName") for state in routers
                         for r in routers[state]), ""),
        "days": (inventory.get("utilization") or {}).get("days"),
        "ports": len(inventory["ports"]),
        "categories": CATEGORIES,
        "billing": billing and {
            "currency": billing["currency"],
            "runRate": billing["run_rate"],
            "last12": billing["last12_total"],
            "credits": billing["last12_credits"],
            "latest": (billing.get("latest_invoice") or {}).get("transactionDate"),
            "monthly": billing["monthly"],
            "items": billing["current_items"],
        },
        "util": util_rows,
        "sizing": sizing,
        "findings": order_findings(findings(billing, util_rows, sizing, active)
                                   + [dict(zip(("tone", "title", "body"), f)) for f in
                                      reliability_findings(health, routes, sizing)]),
        "reliability": health,
        "routes": routes,
        "footprint": {
            "active": len(active),
            "cloudLinks": len(cloud_links),
            "cloudCapacity": sum(r["mbps"] for r in cloud_links),
            "peakCloud": round(peak_total, 1),
            "routers": len(routers["active"]),
            "neNodes": node_count,
        },
        "connections": conn_rows,
        "routers": [router_row(r, s) for s in ("active", "deprovisioned")
                    for r in routers[s]],
        "devices": devices,
    }


def render_html(inventory):
    data = json.dumps(build_data(inventory), default=str).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<title>Equinix Interconnect Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
/* Layout: one column, leadership summary first (KPIs, findings), then spend, utilization, right-sizing, inventory. Charts scale to their card width. */
:root {
  --bg: #f4f6f7; --surface: #ffffff; --fg: #141c24; --fg2: #46535f; --muted: #6f7b86;
  --line: #e1e5e8; --axis: #c3c9ce; --accent: #0b6e85; --accent-soft: #e2f1f4;
  --good: #0f7a3d; --good-soft: #e2f3e8; --warn: #9a5b00; --warn-soft: #fbf0dc;
  --off: #6b7580; --off-soft: #eceff2;
  /* status marks (fixed across themes) */
  --crit: #d03b3b; --warn-mark: #fab219;
  /* categorical slots 1-5, validated order */
  --c-gcp: #2a78d6; --c-aws: #eb6834; --c-ne: #1baf7a; --c-cr: #eda100; --c-inet: #e87ba4; --c-other: #9aa3ab;
  --display: "IBM Plex Sans Condensed", "Arial Narrow", sans-serif;
  --body: "IBM Plex Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0e1418; --surface: #161e25; --fg: #e8edf1; --fg2: #b4c0ca; --muted: #8b98a3;
  --line: #26313a; --axis: #3a4650; --accent: #4fc0d8; --accent-soft: #143440;
  --good: #3fcf78; --good-soft: #14321f; --warn: #f0b45a; --warn-soft: #3a2b12;
  --off: #8d98a2; --off-soft: #222c35;
  --c-gcp: #3987e5; --c-aws: #d95926; --c-ne: #199e70; --c-cr: #c98500; --c-inet: #d55181; --c-other: #6c7680;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0e1418; --surface: #161e25; --fg: #e8edf1; --fg2: #b4c0ca; --muted: #8b98a3;
  --line: #26313a; --axis: #3a4650; --accent: #4fc0d8; --accent-soft: #143440;
  --good: #3fcf78; --good-soft: #14321f; --warn: #f0b45a; --warn-soft: #3a2b12;
  --off: #8d98a2; --off-soft: #222c35;
  --c-gcp: #3987e5; --c-aws: #d95926; --c-ne: #199e70; --c-cr: #c98500; --c-inet: #d55181; --c-other: #6c7680;
  color-scheme: dark; }

* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 14px;
  line-height: 1.5; padding-inline: 16px; padding-block: 28px 56px; }
.wrap { max-width: 1180px; margin-inline: auto; display: grid; gap: 36px; }
header { display: grid; gap: 14px; }
.title-row { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
h1 { font-family: var(--display); font-weight: 600; font-size: 34px; line-height: 1.05; margin: 0;
  letter-spacing: -0.01em; text-wrap: balance; }
h2 { font-family: var(--display); font-weight: 600; font-size: 22px; margin: 0; text-wrap: balance; }
h3 { font-size: 13px; font-weight: 600; margin: 0; }
.eyebrow { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
.meta { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 4px 18px; }
.meta b { color: var(--fg); font-weight: 500; }
nav { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 13px; border-top: 1px solid var(--line); padding-top: 10px; }
nav a { color: var(--fg2); text-decoration: none; }
nav a:hover, nav a:focus-visible { color: var(--accent); text-decoration: underline; }
section { display: grid; gap: 14px; min-width: 0; scroll-margin-top: 16px; }
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 6px 16px; }
.lede { color: var(--fg2); margin: 0; max-width: 72ch; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; min-width: 0; }
.card-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; align-items: baseline; margin-bottom: 10px; }
.card-head .sub { color: var(--muted); font-size: 12.5px; }
.grid2 { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); gap: 16px; }
@media (max-width: 860px) { .grid2 { grid-template-columns: minmax(0, 1fr); } }

/* KPI strip */
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1px;
  background: var(--line); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.kpi { background: var(--surface); padding: 16px 18px; display: grid; gap: 4px; align-content: start; min-width: 0; }
.kpi .num { font-family: var(--display); font-size: 32px; font-weight: 600; line-height: 1.1; }
.kpi .num small { font-size: 15px; font-weight: 500; color: var(--muted); margin-left: 3px; }
.kpi .sub { color: var(--fg2); font-size: 12.5px; }
.kpi .delta { font-size: 12.5px; font-weight: 600; }
.delta.good { color: var(--good); } .delta.warn { color: var(--warn); }

/* Findings */
.findings { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.finding { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px;
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; align-content: start; }
.finding .icon { grid-row: span 2; width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center;
  font-size: 13px; font-weight: 700; }
.finding.good .icon { color: var(--good); background: var(--good-soft); }
.finding.warn .icon { color: var(--warn); background: var(--warn-soft); }
.finding.info .icon { color: var(--accent); background: var(--accent-soft); }
.finding h3 { font-size: 14.5px; line-height: 1.35; }
.finding p { margin: 0; color: var(--fg2); font-size: 13px; }

/* Charts */
.chart { position: relative; width: 100%; }
.chart svg { display: block; width: 100%; overflow: visible; }
.axis text, .tick { fill: var(--muted); font-size: 11px; font-family: var(--body); font-variant-numeric: tabular-nums; }
.grid line { stroke: var(--line); stroke-width: 1; }
.baseline { stroke: var(--axis); stroke-width: 1; }
.cap-label { fill: var(--fg); font-size: 12px; font-weight: 600; font-family: var(--body); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12.5px; color: var(--fg2); margin-top: 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; flex: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.note { color: var(--muted); font-size: 12.5px; margin: 8px 0 0; }
.tip { position: absolute; pointer-events: none; z-index: 5; background: var(--surface); color: var(--fg);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: 12px; min-width: 150px;
  box-shadow: 0 6px 20px rgba(10, 20, 30, 0.14); display: grid; gap: 3px; }
.tip .t-head { color: var(--muted); font-size: 11.5px; margin-bottom: 2px; }
.tip .t-row { display: flex; align-items: center; gap: 6px; }
.tip .t-row b { margin-left: auto; padding-left: 12px; font-variant-numeric: tabular-nums; }
.hbars { display: grid; gap: 7px; }
.hbar { display: grid; grid-template-columns: minmax(0, 11.5em) minmax(0, 1fr) auto; gap: 10px; align-items: center;
  font-size: 12.5px; border-radius: 6px; padding: 1px 2px; }
.hbar:hover, .hbar:focus-visible { background: color-mix(in srgb, var(--accent-soft) 55%, transparent); outline: none; }
.hbar .lab { overflow-wrap: anywhere; line-height: 1.25; color: var(--fg2); display: flex; align-items: center; gap: 6px; }
.hbar .track { height: 14px; }
.hbar .bar { height: 100%; border-radius: 0 4px 4px 0; min-width: 2px; }
.hbar .val { font-variant-numeric: tabular-nums; font-weight: 600; text-align: right; min-width: 4.5em; }
.multiples { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); gap: 12px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px 8px; min-width: 0; }
.panel-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.panel-head h3 { display: flex; align-items: center; gap: 7px; }
.panel-head .sub { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
details.tv { margin-top: 10px; }
details.tv summary { cursor: pointer; color: var(--accent); font-size: 12.5px; width: max-content; }

/* Tables */
.frame { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
.frame.flat { border: 0; border-radius: 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
  font-weight: 600; padding: 9px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th.sortable { cursor: pointer; user-select: none; }
th[aria-sort] { color: var(--accent); }
th[aria-sort="ascending"]::after { content: " ▲"; font-size: 9px; }
th[aria-sort="descending"]::after { content: " ▼"; font-size: 9px; }
td { padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: middle; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tfoot td { border-top: 1px solid var(--axis); border-bottom: 0; font-weight: 600; }
tbody tr:hover td { background: color-mix(in srgb, var(--accent-soft) 40%, transparent); }
tr.gone td { color: var(--muted); }
tr.gone td.name { text-decoration: line-through; text-decoration-color: var(--off); }
.name { font-weight: 600; }
.mono, td.date { font-family: var(--mono); font-size: 12.5px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.ep { display: grid; line-height: 1.3; }
.ep small { font-family: var(--mono); font-size: 10.5px; color: var(--muted); }
.arrow { color: var(--muted); padding-inline: 0; }
.meter { display: flex; align-items: center; gap: 8px; min-width: 150px; }
.meter .track { flex: 1; height: 6px; border-radius: 3px; background: var(--line); overflow: hidden; }
.meter .fill { height: 100%; border-radius: 3px; background: var(--accent); min-width: 2px; }
.meter span { font-variant-numeric: tabular-nums; min-width: 3.6em; text-align: right; }
.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600;
  padding: 2px 8px; border-radius: 999px; letter-spacing: 0.02em; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--good); background: var(--good-soft); }
.pill.off { color: var(--off); background: var(--off-soft); }
.pill.warn { color: var(--warn); background: var(--warn-soft); }
.pill.info { color: var(--accent); background: var(--accent-soft); }
.cloud { display: inline-flex; align-items: center; gap: 6px; }
.prio { font-family: var(--mono); font-size: 11px; color: var(--muted); }
.saving { color: var(--good); font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 6px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; background: var(--surface); }
.seg button { font: inherit; font-size: 12.5px; border: 0; background: transparent; color: var(--muted); padding: 5px 11px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
button:focus-visible, input:focus-visible, th:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
input[type=search] { font: inherit; font-size: 13px; padding: 5px 10px; border: 1px solid var(--line);
  border-radius: 7px; background: var(--surface); color: var(--fg); width: 210px; max-width: 100%; }
.count { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.empty { padding: 18px; color: var(--muted); }
ul.method { margin: 0; padding-left: 18px; color: var(--fg2); font-size: 13px; display: grid; gap: 4px; max-width: 90ch; }
.lane-label { fill: var(--fg2); font-size: 12px; font-family: var(--body); }
.status-crit { fill: var(--crit); } .status-warn { fill: var(--warn-mark); }
.reset-line { stroke: var(--axis); stroke-width: 1; }
.prefix-note { display: block; font-size: 11.5px; color: var(--muted); margin-top: 2px; white-space: normal; }
footer { color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
@media (max-width: 560px) { h1 { font-size: 27px; } .kpi .num { font-size: 27px; } input[type=search] { width: 100%; } }
@media (prefers-reduced-motion: no-preference) { .hbar .bar, .meter .fill { transition: width .3s ease; } }

/* PDF mode (#pdf): lay out at the printed page width so charts are drawn at final size. */
html.pdf body { padding: 0; }
html.pdf .wrap, html.pdf section, html.pdf header { display: block; max-width: none; }
html.pdf .wrap > * + *, html.pdf section > * + * { margin-top: 16px; }
html.pdf header > * + * { margin-top: 12px; }
html.pdf .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
/* Chrome overlaps content when a grid row is pushed to the next page, so the
   PDF lays cards out as block / inline blocks, which paginate cleanly. */
html.pdf .grid2 { display: block; }
html.pdf .grid2 > .card + .card { margin-top: 16px; }
html.pdf .findings { display: block; }
html.pdf .findings > .finding { display: inline-grid; vertical-align: top;
  width: calc((100% - 24px) / 3); margin: 0 12px 12px 0; }
html.pdf .findings > .finding:nth-child(3n) { margin-right: 0; }
html.pdf .multiples { display: block; }
html.pdf .multiples > .panel { display: inline-block; vertical-align: top;
  width: calc((100% - 12px) / 2); margin: 0 12px 12px 0; }
html.pdf .multiples > .panel:nth-child(2n) { margin-right: 0; }

/* Print / PDF: landscape Letter, light palette, no controls, keep cards whole. */
@page { size: letter landscape; margin: 10mm 11mm; }
@media print {
  :root, :root:not([data-theme="light"]), :root[data-theme="dark"] {
    --bg: #ffffff; --surface: #ffffff; --fg: #141c24; --fg2: #46535f; --muted: #6f7b86;
    --line: #e1e5e8; --axis: #c3c9ce; --accent: #0b6e85; --accent-soft: #e2f1f4;
    --good: #0f7a3d; --good-soft: #e2f3e8; --warn: #9a5b00; --warn-soft: #fbf0dc;
    --off: #6b7580; --off-soft: #eceff2;
    --c-gcp: #2a78d6; --c-aws: #eb6834; --c-ne: #1baf7a; --c-cr: #eda100; --c-inet: #e87ba4; --c-other: #9aa3ab;
    color-scheme: light; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  body { padding: 0; font-size: 12px; }
  .wrap { gap: 20px; max-width: none; }
  nav, .controls, details.tv > summary, .tip { display: none !important; }
  #spend, #utilization, #rightsizing, #reliability, #inventory { break-before: page; }
  .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .sec-head, .card-head, .lede { break-after: avoid; }
  .chart, .kpis, tr, .hbar, .legend { break-inside: avoid; }
  td.date, td.num { white-space: nowrap; }
  .frame { overflow: visible; }
  td, th { white-space: normal; padding: 6px 8px; }
  table { font-size: 11px; }
  .chart svg { height: auto; }
  tbody tr:hover td { background: none; }
}
</style>

<div class="wrap">
  <header>
    <div class="title-row">
      <div>
        <div class="eyebrow">Equinix Fabric &amp; Network Edge</div>
        <h1>Interconnect Report</h1>
      </div>
      <div class="meta" id="meta"></div>
    </div>
    <nav aria-label="Sections">
      <a href="#summary">Summary</a><a href="#spend">Spend</a><a href="#utilization">Utilization</a>
      <a href="#rightsizing">Right-sizing</a><a href="#reliability">Reliability</a>
      <a href="#routing">Routing</a><a href="#inventory">Inventory</a>
    </nav>
  </header>

  <section id="summary">
    <div class="kpis" id="kpis"></div>
    <div class="findings" id="findings"></div>
    <details class="tv" id="more-findings" hidden><summary></summary><div class="findings" id="findings-more" style="margin-top:12px"></div></details>
  </section>

  <section id="spend">
    <div class="sec-head"><h2>Spend</h2><span class="count" id="spend-sub"></span></div>
    <div class="grid2">
      <div class="card">
        <div class="card-head"><h3>Monthly charges by category</h3><span class="sub">Invoice month, before tax</span></div>
        <div class="chart" id="spend-chart"></div>
        <div class="legend" id="spend-legend"></div>
        <p class="note" id="spend-note"></p>
        <details class="tv"><summary>Show as table</summary><div class="frame flat"><table id="spend-table"></table></div></details>
      </div>
      <div class="card">
        <div class="card-head"><h3>Latest invoice by item</h3><span class="sub" id="items-sub"></span></div>
        <div class="hbars chart" id="items"></div>
      </div>
    </div>
  </section>

  <section id="utilization">
    <div class="sec-head"><h2>Utilization</h2><span class="count" id="util-sub"></span></div>
    <p class="lede">Hourly peak throughput per link (the busier direction). All panels share one scale so links compare directly; provisioned capacity is far above it.</p>
    <div class="frame"><table id="util-table"></table></div>
    <div class="multiples" id="multiples"></div>
  </section>

  <section id="rightsizing">
    <div class="sec-head"><h2>Right-sizing</h2><span class="count" id="size-sub"></span></div>
    <div class="frame"><table id="size-table"></table></div>
    <ul class="method" id="method"></ul>
  </section>

  <section id="reliability">
    <div class="sec-head"><h2>Reliability</h2><span class="count" id="rel-sub"></span></div>
    <p class="lede">BGP session drops on each link, from Equinix Cloud Events. Drops under a minute are flaps; longer ones are outages.</p>
    <div class="card">
      <div class="card-head"><h3>Session timeline</h3><span class="sub">UTC</span></div>
      <div class="chart" id="timeline"></div>
      <div class="legend" id="timeline-legend"></div>
      <p class="note" id="timeline-note"></p>
    </div>
    <div class="frame"><table id="rel-table"></table></div>
  </section>

  <section id="routing">
    <div class="sec-head"><h2>Routing</h2><span class="count" id="route-sub"></span></div>
    <p class="lede">Networks the Cloud Router learns over BGP, and whether each one has a backup path.</p>
    <div class="frame"><table id="route-table"></table></div>
  </section>

  <section id="inventory">
    <div class="sec-head"><h2>Inventory</h2></div>
    <div class="card-head"><h3>Network Edge devices</h3><span class="count" id="dev-count"></span></div>
    <div class="frame"><table id="devices"></table></div>
    <div class="card-head" style="margin-top:8px"><h3>Cloud Routers</h3>
      <div class="controls"><div class="seg" data-target="routers" data-key="lifecycle"></div></div></div>
    <div class="frame"><table id="routers"></table></div>
    <div class="card-head" style="margin-top:8px"><h3>Connections <span class="count" id="conn-count"></span></h3>
      <div class="controls">
        <input type="search" id="conn-search" placeholder="Filter by name or endpoint" aria-label="Filter connections">
        <div class="seg" data-target="connections" data-key="cloud"></div>
        <div class="seg" data-target="connections" data-key="lifecycle"></div>
      </div></div>
    <div class="frame"><table id="connections"></table></div>
  </section>

  <footer id="foot"></footer>
</div>

<script>
if (location.hash === "#pdf") document.documentElement.classList.add("pdf");
const D = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const money = v => (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-US");
const bw = m => m == null ? "—" : m >= 1000 ? (m / 1000).toLocaleString() + " Gbps" : (+m).toLocaleString() + " Mbps";
const mbps = v => v >= 1000 ? (v / 1000).toFixed(2) + " Gbps" : v >= 10 ? Math.round(v) + " Mbps" : (+v).toFixed(1) + " Mbps";
const monthName = m => new Date(m + "-01T00:00:00Z").toLocaleString("en-US", {month: "short", timeZone: "UTC"});
const monthYear = m => new Date(m + "-01T00:00:00Z").toLocaleString("en-US", {month: "short", year: "numeric", timeZone: "UTC"});
const CAT_VAR = {"Google Cloud": "--c-gcp", "AWS": "--c-aws", "Network Edge": "--c-ne", "Cloud Router": "--c-cr", "Internet Access": "--c-inet", "Other": "--c-other"};
const CLOUD_VAR = {gcp: "--c-gcp", aws: "--c-aws", internet: "--c-inet"};
const CLOUD_NAME = {gcp: "Google Cloud", aws: "AWS", internet: "Internet", azure: "Azure", oracle: "Oracle", other: "Other"};
const cssVar = v => `var(${v})`;
const cloudChip = c => `<span class="cloud"><span class="dot" style="background:${cssVar(CLOUD_VAR[c] || "--c-other")}"></span>${esc(CLOUD_NAME[c] || c)}</span>`;
const SVGNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}
function niceMax(v, ticks = 4) {
  if (v <= 0) return 1;
  const raw = v / ticks, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
  return { max: step * Math.ceil(v / step), step };
}
// Shared tooltip built with textContent only.
function tooltip(host) {
  const tip = document.createElement("div");
  tip.className = "tip"; tip.hidden = true; host.appendChild(tip);
  return {
    show(x, y, head, rows) {
      tip.replaceChildren();
      const h = document.createElement("div"); h.className = "t-head"; h.textContent = head; tip.appendChild(h);
      for (const [color, label, value] of rows) {
        const r = document.createElement("div"); r.className = "t-row";
        if (color) { const d = document.createElement("span"); d.className = "sw"; d.style.background = color; r.appendChild(d); }
        r.appendChild(document.createTextNode(label));
        const b = document.createElement("b"); b.textContent = value; r.appendChild(b);
        tip.appendChild(r);
      }
      tip.hidden = false;
      const w = host.clientWidth, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(w - tw, x + 12)) + "px";
      tip.style.top = Math.max(0, y - tip.offsetHeight - 10) + "px";
    },
    hide() { tip.hidden = true; },
  };
}
function onResize(el, fn) {
  let w = 0;
  new ResizeObserver(() => { if (el.clientWidth !== w) { w = el.clientWidth; fn(); } }).observe(el);
}

/* ---------- header, KPIs, findings ---------- */
const B = D.billing, U = D.util, S = D.sizing, F = D.footprint;
const winStart = U ? new Date(U.start) : null;
const winEnd = U ? new Date(winStart.getTime() + (U.hours - 1) * 3600e3) : null;
const fmtDay = d => d.toLocaleDateString("en-US", {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"});
$("meta").innerHTML = [
  D.account && `<span>Account <b>${esc(D.account)}</b></span>`,
  U && `<span>Traffic <b>${fmtDay(winStart)} – ${fmtDay(winEnd)}</b></span>`,
  B && B.latest && `<span>Latest invoice <b>${esc(fmtDay(new Date(B.latest + "T00:00:00Z")))}</b></span>`,
  `<span>Generated <b>${esc(D.generated)}</b></span>`,
].filter(Boolean).join("");

const kpis = [];
if (B) {
  const m = B.monthly, first = m[0], last = m[m.length - 1];
  const ch = first && first.total_charges ? (last.total_charges - first.total_charges) / first.total_charges * 100 : 0;
  kpis.push(["Monthly run-rate", money(B.runRate), "",
    Math.abs(ch) >= 1 ? `<span class="delta ${ch < 0 ? "good" : "warn"}">${ch < 0 ? "▼" : "▲"} ${Math.abs(ch).toFixed(0)}% vs ${monthYear(first.month)}</span>` : "Recurring charges, before tax"]);
  kpis.push(["Last 12 months", money(B.last12), "", B.credits ? `Includes ${money(-B.credits)} in credits` : "Invoiced, including tax"]);
}
if (S) kpis.push(["Potential savings", money(S.monthly_savings), "/mo", `${money(S.monthly_savings * 12)} per year`]);
if (U) kpis.push(["Peak traffic per cloud", mbps(F.peakCloud).split(" ")[0], mbps(F.peakCloud).split(" ")[1],
  `Busiest cloud, all its links combined · ${bw(F.cloudCapacity)} provisioned on ${F.cloudLinks} links`]);
const R = D.reliability;
if (R) {
  const mon = R.links.filter(l => l.monitored);
  const decom = new Set((S ? S.links : []).filter(r => r.action === "decommission").map(r => r.uuid));
  const prod = mon.filter(l => !decom.has(l.uuid));
  const worst = prod.reduce((a, b) => (b.availability < a.availability ? b : a), prod[0]);
  if (worst) kpis.push(["Lowest link availability", worst.availability.toFixed(2), "%",
    `${esc(worst.name)} · ${prod.reduce((t, l) => t + l.outages.length, 0)} outages in ${R.days} days`]);
}
kpis.push(["Footprint", F.active, " links", `${F.routers} Cloud Router · ${F.neNodes}-node firewall`]);
$("kpis").innerHTML = kpis.map(([label, num, unit, sub]) =>
  `<div class="kpi"><span class="eyebrow">${label}</span><span class="num">${esc(num)}${unit ? `<small>${esc(unit)}</small>` : ""}</span><span class="sub">${sub}</span></div>`).join("");

const ICON = {good: "✓", warn: "!", info: "i"};
const findingCard = f => `<div class="finding ${f.tone}"><span class="icon" aria-hidden="true">${ICON[f.tone]}</span><h3>${esc(f.title)}</h3><p>${esc(f.body)}</p></div>`;
const TOP = 6;
$("findings").innerHTML = D.findings.slice(0, TOP).map(findingCard).join("");
if (D.findings.length > TOP) {
  $("more-findings").hidden = false;
  $("more-findings").querySelector("summary").textContent = `${D.findings.length - TOP} more findings`;
  $("findings-more").innerHTML = D.findings.slice(TOP).map(findingCard).join("");
}

/* ---------- spend: stacked columns ---------- */
function drawSpend() {
  const host = $("spend-chart");
  host.replaceChildren();
  if (!B) { host.innerHTML = `<p class="empty">Billing data wasn't available for this run.</p>`; return; }
  const cats = D.categories.filter(c => B.monthly.some(m => m.charges[c] > 0));
  const W = host.clientWidth, H = 260, m = {t: 22, r: 8, b: 26, l: 48};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const {max, step} = niceMax(Math.max(...B.monthly.map(x => x.total_charges)));
  const y = v => m.t + ih - v / max * ih;
  const band = iw / B.monthly.length, bwid = Math.min(24, band * 0.6);
  const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Monthly charges by category"}, host);
  const g = svgEl("g", {class: "grid"}, svg), ax = svgEl("g", {class: "axis"}, svg);
  for (let v = 0; v <= max + 1e-9; v += step) {
    if (v > 0) svgEl("line", {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v)}, g);
    svgEl("text", {x: m.l - 8, y: y(v) + 4, "text-anchor": "end"}, ax).textContent = v >= 1000 ? "$" + (v / 1000) + "k" : "$" + v;
  }
  svgEl("line", {class: "baseline", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0)}, svg);
  const tip = tooltip(host);
  const every = band < 34 ? 2 : 1;
  B.monthly.forEach((mo, i) => {
    const cx = m.l + band * i + band / 2, x0 = cx - bwid / 2;
    let acc = 0;
    const segs = cats.filter(c => mo.charges[c] > 0);
    segs.forEach((c, j) => {
      const v = mo.charges[c], top = y(acc + v), bot = y(acc);
      const isTop = j === segs.length - 1, gap = j > 0 ? 2 : 0;
      const h = Math.max(0, bot - top - gap), r = isTop ? Math.min(4, h) : 0;
      const yb = bot - gap, yt = yb - h;
      svgEl("path", {fill: cssVar(CAT_VAR[c]), d: `M${x0},${yb}V${yt + r}Q${x0},${yt} ${x0 + r},${yt}H${x0 + bwid - r}Q${x0 + bwid},${yt} ${x0 + bwid},${yt + r}V${yb}Z`}, svg);
      acc += v;
    });
    if (i % every === (B.monthly.length - 1) % every)
      svgEl("text", {x: cx, y: H - 8, "text-anchor": "middle"}, ax).textContent = monthName(mo.month);
    if (i === B.monthly.length - 1)
      svgEl("text", {class: "cap-label", x: cx, y: y(acc) - 7, "text-anchor": "middle"}, svg).textContent = money(acc);
    const hit = svgEl("rect", {x: m.l + band * i, y: m.t, width: band, height: ih, fill: "transparent", tabindex: 0,
      "aria-label": `${monthYear(mo.month)}: ${money(mo.total_charges)}`}, svg);
    const show = ev => {
      const rows = [...segs].reverse().map(c => [cssVar(CAT_VAR[c]), c, money(mo.charges[c])]);
      rows.push([null, "Total charges", money(mo.total_charges)]);
      if (mo.credits) rows.push([null, "Credits", money(mo.credits)]);
      const r = host.getBoundingClientRect();
      tip.show(ev.clientX ? ev.clientX - r.left : cx, ev.clientY ? ev.clientY - r.top : y(acc), monthYear(mo.month), rows);
    };
    hit.addEventListener("pointermove", show); hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", tip.hide); hit.addEventListener("blur", tip.hide);
  });
  $("spend-legend").innerHTML = cats.map(c => `<span><span class="sw" style="background:${cssVar(CAT_VAR[c])}"></span>${esc(c)}</span>`).join("");
}
if (B) {
  const credits = B.monthly.filter(m => m.credits);
  $("spend-note").textContent = credits.length
    ? "Credits are not drawn: " + credits.map(m => `${money(m.credits)} on the ${monthYear(m.month)} invoice`).join(", ") + "."
    : "";
  $("spend-sub").textContent = `${B.monthly.length} invoices · ${B.currency}`;
  const cats = D.categories.filter(c => B.monthly.some(m => m.charges[c] > 0));
  $("spend-table").innerHTML = `<thead><tr><th>Month</th>${cats.map(c => `<th class="num">${esc(c)}</th>`).join("")}<th class="num">Charges</th><th class="num">Credits</th></tr></thead><tbody>` +
    B.monthly.map(m => `<tr><td>${monthYear(m.month)}</td>${cats.map(c => `<td class="num">${money(m.charges[c])}</td>`).join("")}<td class="num"><b>${money(m.total_charges)}</b></td><td class="num">${m.credits ? money(m.credits) : "—"}</td></tr>`).join("") + "</tbody>";

  // Latest invoice by item: horizontal bars, value at the tip.
  const items = B.items, maxItem = Math.max(...items.map(i => i.amount)), total = items.reduce((t, i) => t + i.amount, 0);
  $("items-sub").textContent = `${money(total)} · ${monthYear(B.latest.slice(0, 7))}`;
  const host = $("items"), tip = tooltip(host);
  items.forEach(it => {
    const row = document.createElement("div");
    row.className = "hbar"; row.tabIndex = 0;
    row.innerHTML = `<span class="lab"><span class="sw" style="background:${cssVar(CAT_VAR[it.category])}"></span><span></span></span><span class="track"><span class="bar" style="display:block;width:${it.amount / maxItem * 100}%;background:${cssVar(CAT_VAR[it.category])}"></span></span><span class="val">${money(it.amount)}</span>`;
    row.querySelector(".lab span:last-child").textContent = it.item;
    const show = () => tip.show(row.offsetLeft + row.offsetWidth * 0.4, row.offsetTop, it.item,
      [[cssVar(CAT_VAR[it.category]), it.category, money(it.amount)], [null, "Share of invoice", (it.amount / total * 100).toFixed(1) + "%"]]);
    row.addEventListener("pointerenter", show); row.addEventListener("focus", show);
    row.addEventListener("pointerleave", tip.hide); row.addEventListener("blur", tip.hide);
    host.appendChild(row);
  });
} else {
  $("items").innerHTML = `<p class="empty">Billing data wasn't available for this run.</p>`;
}
onResize($("spend-chart"), drawSpend);

/* ---------- utilization ---------- */
if (U) {
  $("util-sub").textContent = `${U.links.length} links · last ${D.days} days · 5-minute samples`;
  const hdr = ["Link", "Cloud", "Role", "Capacity", "Average", "95th pct", "Peak", "Peak vs capacity", "Monthly cost"];
  $("util-table").innerHTML = `<thead><tr>${hdr.map((h, i) => `<th class="${i >= 3 && i !== 7 ? "num" : ""}">${h}</th>`).join("")}</tr></thead><tbody>` +
    U.links.map(l => `<tr><td class="name">${esc(l.name)}</td><td>${cloudChip(l.cloud)}</td><td><span class="prio">${esc(l.priority || "—")}</span></td>
      <td class="num">${bw(l.bandwidth)}</td><td class="num">${mbps(l.mean)}</td><td class="num">${mbps(l.p95)}</td><td class="num"><b>${mbps(l.peak)}</b></td>
      <td><div class="meter"><div class="track"><div class="fill" style="width:${Math.min(100, l.peak_pct)}%"></div></div><span>${l.peak_pct < 0.1 ? "<0.1" : l.peak_pct.toFixed(1)}%</span></div></td>
      <td class="num">${l.cost != null ? money(l.cost) : "—"}</td></tr>`).join("") + "</tbody>";

  const shared = niceMax(Math.max(...U.links.map(l => l.peak)) || 1, 2);
  const panels = U.links.map(l => {
    const p = document.createElement("div");
    p.className = "panel";
    p.innerHTML = `<div class="panel-head"><h3><span class="dot" style="background:${cssVar(CLOUD_VAR[l.cloud] || "--c-other")}"></span><span></span></h3><span class="sub">peak ${mbps(l.peak)} · ${bw(l.bandwidth)} link</span></div><div class="chart"></div>`;
    p.querySelector("h3 span:last-child").textContent = l.name;
    $("multiples").appendChild(p);
    return [p.querySelector(".chart"), l];
  });
  const drawPanel = (host, l) => {
    host.replaceChildren();
    const W = host.clientWidth, H = 150, m = {t: 10, r: 6, b: 22, l: 46};
    const iw = W - m.l - m.r, ih = H - m.t - m.b, n = U.hours;
    const x = i => m.l + i / (n - 1) * iw, y = v => m.t + ih - Math.min(v, shared.max) / shared.max * ih;
    const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": `${l.name} hourly peak throughput`}, host);
    const g = svgEl("g", {class: "grid"}, svg), ax = svgEl("g", {class: "axis"}, svg);
    for (let v = 0; v <= shared.max + 1e-9; v += shared.step) {
      if (v > 0) svgEl("line", {x1: m.l, x2: W - m.r, y1: y(v), y2: y(v)}, g);
      svgEl("text", {x: m.l - 6, y: y(v) + 4, "text-anchor": "end"}, ax).textContent = v >= 1000 ? (v / 1000) + "G" : v + "M";
    }
    // month ticks
    const t0 = new Date(U.start).getTime();
    for (let d = new Date(U.start); d.getTime() <= t0 + (n - 1) * 3600e3; d.setUTCDate(d.getUTCDate() + 1)) {
      if (d.getUTCDate() !== 1 || d.getUTCHours() !== 0) continue;
      const i = (d.getTime() - t0) / 3600e3;
      svgEl("text", {x: x(i), y: H - 6, "text-anchor": "middle"}, ax).textContent = d.toLocaleString("en-US", {month: "short", timeZone: "UTC"});
    }
    svgEl("line", {class: "baseline", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0)}, svg);
    const val = i => Math.max(l.in[i] ?? 0, l.out[i] ?? 0);
    let line = "", started = false;
    for (let i = 0; i < n; i++) {
      if (l.in[i] == null && l.out[i] == null) { started = false; continue; }
      line += (started ? "L" : "M") + x(i).toFixed(1) + "," + y(val(i)).toFixed(1); started = true;
    }
    const color = cssVar(CLOUD_VAR[l.cloud] || "--c-other");
    svgEl("path", {d: line + `L${x(n - 1)},${y(0)}L${x(0)},${y(0)}Z`, fill: color, "fill-opacity": 0.1, stroke: "none"}, svg);
    svgEl("path", {d: line, fill: "none", stroke: color, "stroke-width": 1.5, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
    let pk = 0; for (let i = 1; i < n; i++) if (val(i) > val(pk)) pk = i;
    svgEl("circle", {cx: x(pk), cy: y(val(pk)), r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2}, svg);
    const cross = svgEl("line", {x1: 0, x2: 0, y1: m.t, y2: m.t + ih, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden"}, svg);
    const dot = svgEl("circle", {r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden"}, svg);
    const tip = tooltip(host);
    const hit = svgEl("rect", {x: m.l, y: 0, width: iw, height: H, fill: "transparent"}, svg);
    hit.addEventListener("pointermove", ev => {
      const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
      const i = Math.max(0, Math.min(n - 1, Math.round((px - m.l) / iw * (n - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(val(i))); dot.setAttribute("visibility", "visible");
      const when = new Date(t0 + i * 3600e3).toLocaleString("en-US", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false}) + " UTC";
      tip.show(x(i) * r.width / W, 20, when, [[null, "Inbound", l.in[i] == null ? "—" : mbps(l.in[i])], [null, "Outbound", l.out[i] == null ? "—" : mbps(l.out[i])],
        [null, "of capacity", (val(i) / l.bandwidth * 100).toFixed(2) + "%"]]);
    });
    hit.addEventListener("pointerleave", () => { tip.hide(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
  };
  panels.forEach(([host, l]) => onResize(host, () => drawPanel(host, l)));
} else {
  $("util-table").innerHTML = `<tbody><tr><td class="empty">Utilization data wasn't available for this run.</td></tr></tbody>`;
}

/* ---------- right-sizing ---------- */
if (S && S.links.length) {
  const ACT = {decommission: ["warn", "Decommission"], downsize: ["info", "Downsize"], keep: ["ok", "Keep"]};
  $("size-sub").textContent = `${money(S.monthly_savings)}/mo · ${money(S.monthly_savings * 12)}/yr`;
  const hdr = ["Link", "Cloud", "Action", "Current", "Pair peak", "Needed", "Recommended", "Cost now", "Cost after", "Savings / mo"];
  $("size-table").innerHTML = `<thead><tr>${hdr.map((h, i) => `<th class="${i >= 3 ? "num" : ""}">${h}</th>`).join("")}</tr></thead><tbody>` +
    S.links.map(r => `<tr><td class="name">${esc(r.name)}</td><td>${cloudChip(r.cloud)}</td><td><span class="pill ${ACT[r.action][0]}">${ACT[r.action][1]}</span>${r.action === "decommission" && r.prefixes && r.prefixes.length ? `<span class="prefix-note">Still carries ${esc(r.prefixes.join(", "))}</span>` : ""}</td>
      <td class="num">${bw(r.bandwidth)}</td><td class="num">${mbps(r.combined_peak)}</td><td class="num">${mbps(r.needed)}</td>
      <td class="num"><b>${r.recommended ? bw(r.recommended) : "—"}</b></td><td class="num">${money(r.current_cost)}</td><td class="num">${money(r.new_cost)}</td>
      <td class="num ${r.savings > 0 ? "saving" : ""}">${money(r.savings)}</td></tr>`).join("") +
    `</tbody><tfoot><tr><td colspan="7">Total</td><td class="num">${money(S.links.reduce((t, r) => t + r.current_cost, 0))}</td><td class="num">${money(S.links.reduce((t, r) => t + r.new_cost, 0))}</td><td class="num saving">${money(S.monthly_savings)}</td></tr></tfoot>`;
  const tiers = Object.entries(S.tiers).map(([b, p]) => `${bw(+b)} ${money(p)}`).join(" · ");
  $("method").innerHTML = [
    `Links are sized in redundant pairs (same cloud and bandwidth). Each link must be able to carry the pair's combined peak on its own if its partner fails, plus ${Math.round((S.headroom - 1) * 100)}% headroom ("Needed").`,
    `A pair whose combined peak stayed under ${S.idle_mbps} Mbps over the last ${D.days} days is marked for decommissioning. Confirm it isn't a deliberate standby path first.`,
    `Cost after uses Equinix list prices for this metro: ${esc(tiers)} per month.`,
    `Peaks are 5-minute averages, so very short bursts can exceed them. Review month-end and quarterly jobs before downsizing.`,
    `Changing an Equinix tier usually means matching the cloud side as well: the AWS hosted connection capacity, or the Google VLAN attachment capacity.`,
  ].map(t => `<li>${t}</li>`).join("");
} else {
  $("size-table").innerHTML = `<tbody><tr><td class="empty">No right-sizing data for this run.</td></tr></tbody>`;
}

/* ---------- reliability ---------- */
const dur = s => s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const when = iso => new Date(iso).toLocaleString("en-US", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC"});
if (R) {
  const mon = R.links.filter(l => l.monitored);
  const t0 = new Date(R.start).getTime(), t1 = new Date(R.end).getTime();
  $("rel-sub").textContent = `${mon.length} BGP sessions · last ${R.days} days`;
  const drawTimeline = () => {
    const host = $("timeline");
    host.replaceChildren();
    const W = host.clientWidth, lane = 30, m = {t: 18, r: 8, b: 24, l: Math.min(190, W * 0.38)};
    const H = m.t + m.b + lane * mon.length, iw = W - m.l - m.r;
    const x = t => m.l + (t - t0) / (t1 - t0) * iw;
    const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "BGP session outages and flaps per link"}, host);
    const g = svgEl("g", {class: "grid"}, svg), ax = svgEl("g", {class: "axis"}, svg);
    for (let d = new Date(t0); d.getTime() <= t1; d.setUTCDate(d.getUTCDate() + 1)) {
      if (d.getUTCDate() !== 1) continue;
      const day = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
      if (day < t0) continue;
      svgEl("line", {x1: x(day), x2: x(day), y1: m.t, y2: H - m.b}, g);
      svgEl("text", {x: x(day), y: H - 6, "text-anchor": "middle"}, ax).textContent = new Date(day).toLocaleString("en-US", {month: "short", timeZone: "UTC"});
    }
    const tip = tooltip(host);
    const hover = (el, head, rows) => {
      el.setAttribute("tabindex", 0);
      const show = ev => { const r = host.getBoundingClientRect(); const b = el.getBoundingClientRect();
        tip.show((ev && ev.clientX ? ev.clientX : b.left + b.width / 2) - r.left, b.top - r.top, head, rows); };
      el.addEventListener("pointerenter", show); el.addEventListener("focus", () => show());
      el.addEventListener("pointerleave", tip.hide); el.addEventListener("blur", tip.hide);
    };
    R.network_resets.forEach(iso => {
      const xx = x(new Date(iso).getTime());
      svgEl("line", {class: "reset-line", x1: xx, x2: xx, y1: m.t - 6, y2: H - m.b}, svg);
      const mk = svgEl("path", {d: `M${xx - 4},${m.t - 12}L${xx + 4},${m.t - 12}L${xx},${m.t - 5}Z`, fill: "var(--muted)"}, svg);
      hover(mk, when(iso) + " UTC", [[null, "Network-wide reset", "all sessions"]]);
    });
    mon.forEach((l, i) => {
      const y = m.t + lane * i + lane / 2;
      svgEl("line", {class: "baseline", x1: m.l, x2: W - m.r, y1: y, y2: y}, svg);
      const lab = svgEl("text", {class: "lane-label", x: m.l - 10, y: y + 4, "text-anchor": "end"}, svg);
      lab.textContent = l.name.length > 26 && m.l < 180 ? l.name.slice(0, 24) + "…" : l.name;
      l.flaps.forEach(f => {
        const xx = x(new Date(f.start).getTime());
        const mk = svgEl("rect", {class: "status-warn", x: xx - 1.5, y: y - 7, width: 3, height: 14, rx: 1.5}, svg);
        hover(mk, l.name, [[cssVar("--warn-mark"), "Flap", dur(f.seconds)], [null, "At", when(f.start) + " UTC"]]);
      });
      l.outages.forEach(o => {
        const a = x(new Date(o.start).getTime()), b = x(new Date(o.end).getTime());
        const w = Math.max(8, b - a);
        const mk = svgEl("rect", {class: "status-crit", x: a - (w - (b - a)) / 2, y: y - 9, width: w, height: 18, rx: 4,
          stroke: "var(--surface)", "stroke-width": 2}, svg);
        hover(mk, l.name, [[cssVar("--crit"), "Outage", dur(o.seconds)], [null, "From", when(o.start) + " UTC"],
          [null, "Link status", o.link_down ? "went down" : "stayed up"]]);
      });
    });
  };
  onResize($("timeline"), drawTimeline);
  $("timeline-legend").innerHTML = `<span><span class="sw" style="background:var(--crit);width:16px"></span>Outage (${R.flap_seconds} s or longer)</span>
    <span><span class="sw" style="background:var(--warn-mark);width:3px;height:12px"></span>Flap (under ${R.flap_seconds} s)</span>
    <span><span style="color:var(--muted)">▼</span>Network-wide reset</span>`;
  $("timeline-note").textContent = R.network_resets.length
    ? `${R.network_resets.length} network-wide resets (${R.network_resets.map(t => when(t).split(",")[0]).join(", ")}): every session re-established at once with no drop recorded, consistent with Equinix platform maintenance.`
    : "";
  const hdr = ["Link", "Cloud", "Now", "Availability", "Outages", "Downtime", "Longest drop", "Flaps", "BFD"];
  const rows = [...mon].sort((a, b) => a.availability - b.availability);
  $("rel-table").innerHTML = `<thead><tr>${hdr.map((h, i) => `<th class="${i >= 3 && i <= 7 ? "num" : ""}">${h}</th>`).join("")}</tr></thead><tbody>` +
    rows.map(l => `<tr><td class="name">${esc(l.name)}</td><td>${cloudChip(l.cloud)}</td>
      <td>${l.status ? `<span class="pill ${l.status === "UP" ? "ok" : "warn"}">${esc(l.status)}</span>` : "—"}</td>
      <td class="num"><b>${l.availability.toFixed(3)}%</b></td><td class="num">${l.outages.length}</td><td class="num">${dur(l.downtime_seconds)}</td>
      <td class="num">${l.longest_seconds ? dur(l.longest_seconds) : "—"}</td><td class="num">${l.flaps.length}</td>
      <td>${l.bfd == null ? "—" : `<span class="pill ${l.bfd ? "ok" : "off"}">${l.bfd ? "On" : "Off"}</span>`}</td></tr>`).join("") + "</tbody>";
} else {
  $("rel-table").innerHTML = `<tbody><tr><td class="empty">Event data wasn't available for this run.</td></tr></tbody>`;
  $("timeline").innerHTML = "";
}

/* ---------- routing ---------- */
if (D.routes && D.routes.length) {
  const single = D.routes.filter(r => !r.redundant).length;
  $("route-sub").textContent = `${D.routes.length} networks · ${single ? single + " without a backup path" : "all with a backup path"}`;
  $("route-table").innerHTML = `<thead><tr><th>Network</th><th>Cloud</th><th>Backup path</th><th>Learned via</th><th>Next hops</th><th class="num">Origin ASN</th></tr></thead><tbody>` +
    D.routes.map(r => `<tr><td class="mono name">${esc(r.prefix)}</td><td>${cloudChip(r.cloud)}</td>
      <td><span class="pill ${r.redundant ? "ok" : "warn"}">${r.redundant ? r.paths.length + " paths" : "Single path"}</span></td>
      <td>${r.paths.map(p => esc(p.connection)).join("<br>")}</td><td class="mono">${r.paths.map(p => esc(p.next_hop)).join("<br>")}</td>
      <td class="num mono">${esc(r.origin_asn)}</td></tr>`).join("") + "</tbody>";
} else {
  $("route-table").innerHTML = `<tbody><tr><td class="empty">Route data wasn't available for this run.</td></tr></tbody>`;
}

/* ---------- inventory tables ---------- */
const GOOD = new Set(["PROVISIONED", "ACTIVE", "AVAILABLE"]);
const GONE = new Set(["DEPROVISIONED", "DELETED", "NOT_PROVISIONED"]);
const pill = s => `<span class="pill ${GOOD.has(s) ? "ok" : GONE.has(s) ? "off" : "warn"}">${esc(s || "UNKNOWN")}</span>`;
const KIND = {VD: "Edge device", CLOUD_ROUTER: "Cloud router", SP: "Service profile", COLO: "Port", NETWORK: "Network"};
const ep = e => `<span class="ep">${esc(e.name)}<small>${esc(KIND[e.kind] || e.kind)}</small></span>`;
const count = (rows, k, v) => rows.filter(r => r[k] === v).length;
const TABLES = {
  devices: { rows: D.devices, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["Nodes", "nodes", r => esc(r.nodes)],
    ["Status", "status", r => pill(r.status)],
    ["Type", "type", r => esc(r.type)],
    ["Vendor", "vendor", r => esc(r.vendor)],
    ["Metro", "metro", r => `<span class="mono">${esc(r.metro)}</span> ${esc(r.metroName)}`],
    ["IBX", "ibx", r => esc(r.ibx), "mono"],
    ["Created", "created", r => esc(r.created), "date"],
  ]},
  routers: { rows: D.routers, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["State", "state", r => pill(r.state)],
    ["Package", "package", r => esc(r.package), "mono"],
    ["Metro", "metro", r => `<span class="mono">${esc(r.metro)}</span> ${esc(r.metroName)}`],
    ["ASN", "asn", r => esc(r.asn), "num"],
    ["Conns", "conns", r => esc(r.conns), "num"],
    ["Created", "created", r => esc(r.created), "date"],
    ["Updated / deleted", r => r.deleted || r.updated, r => esc(r.deleted || r.updated), "date"],
  ]},
  connections: { rows: D.connections, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["Status", "status", r => pill(r.status)],
    ["Cloud", "cloud", r => cloudChip(r.cloud)],
    ["Bandwidth", "mbps", r => bw(r.mbps), "num"],
    ["Role", "priority", r => `<span class="prio">${esc(r.priority || "—")}</span>`],
    ["A-side", r => r.a.name, r => ep(r.a)],
    ["", null, () => "→", "arrow"],
    ["Z-side", r => r.z.name, r => ep(r.z)],
    ["Type", "type", r => esc(r.type), "mono"],
    ["Created", "created", r => esc(r.created), "date"],
    ["Updated / deleted", r => r.deleted || r.updated, r => esc(r.deleted || r.updated), "date"],
  ]},
};
const state = {
  devices: { sort: null, dir: 1, filters: {} },
  routers: { sort: null, dir: 1, filters: { lifecycle: "active" } },
  connections: { sort: null, dir: 1, filters: { lifecycle: "active", cloud: "all" }, q: "" },
};
const valueOf = (key, r) => typeof key === "function" ? key(r) : r[key];
function render(id) {
  const def = TABLES[id], st = state[id];
  let rows = def.rows.filter(r => Object.entries(st.filters).every(([k, v]) => v === "all" || r[k] === v));
  if (st.q) {
    const q = st.q.toLowerCase();
    rows = rows.filter(r => [r.name, r.a?.name, r.z?.name].some(v => String(v || "").toLowerCase().includes(q)));
  }
  if (st.sort !== null) {
    const key = def.cols[st.sort][1];
    rows = [...rows].sort((x, y) => {
      const a = valueOf(key, x), b = valueOf(key, y);
      return (typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b))) * st.dir;
    });
  }
  const head = "<thead><tr>" + def.cols.map(([h, key, , cls], i) =>
    `<th class="${cls || ""} ${key === null ? "" : "sortable"}" ${key === null ? "" : `data-i="${i}" tabindex="0"`} ${st.sort === i ? `aria-sort="${st.dir > 0 ? "ascending" : "descending"}"` : ""}>${h}</th>`).join("") + "</tr></thead>";
  const body = rows.length
    ? rows.map(r => `<tr class="${r.lifecycle === "deprovisioned" ? "gone" : ""}">` +
        def.cols.map(([, , cell, cls]) => `<td class="${cls || ""}">${cell(r)}</td>`).join("") + "</tr>").join("")
    : `<tr><td class="empty" colspan="${def.cols.length}">No ${id} match these filters.</td></tr>`;
  const table = $(id);
  table.innerHTML = head + "<tbody>" + body + "</tbody>";
  table.querySelectorAll("th[data-i]").forEach(th => {
    const go = () => { const i = +th.dataset.i; st.dir = st.sort === i ? -st.dir : 1; st.sort = i; render(id); };
    th.onclick = go;
    th.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  });
  const el = {devices: "dev-count", connections: "conn-count"}[id];
  if (el) $(el).textContent = rows.length === def.rows.length ? `${def.rows.length}` : `${rows.length} of ${def.rows.length}`;
}
function segment(el) {
  const id = el.dataset.target, key = el.dataset.key, rows = TABLES[id].rows;
  const opts = key === "lifecycle"
    ? [["active", "Active"], ["deprovisioned", "Deprovisioned"], ["all", "All"]]
    : [["all", "All clouds"], ...[...new Set(rows.map(r => r[key]))].sort().map(v => [v, CLOUD_NAME[v] || v])];
  el.innerHTML = opts.map(([v, label]) => {
    const n = v === "all" ? rows.length : count(rows, key, v);
    return `<button type="button" data-v="${esc(v)}" aria-pressed="${state[id].filters[key] === v}">${esc(label)} <span class="count">${n}</span></button>`;
  }).join("");
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    state[id].filters[key] = b.dataset.v;
    el.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b));
    render(id);
  });
}
document.querySelectorAll(".seg").forEach(segment);
$("conn-search").oninput = e => { state.connections.q = e.target.value.trim(); render("connections"); };
Object.keys(TABLES).forEach(render);
// PDF mode (#pdf): open folded content and show every inventory row.
if (location.hash === "#pdf") {
  // One findings list in the PDF (nothing is folded on paper).
  $("findings").append(...$("findings-more").children);
  $("more-findings").remove();
  // Unwrap <details>: Chrome clips their content at page breaks when printing.
  document.querySelectorAll("details").forEach(d => {
    const box = document.createElement("div");
    box.hidden = d.hidden;
    box.style.marginTop = "12px";
    [...d.children].filter(c => c.tagName !== "SUMMARY").forEach(c => box.appendChild(c));
    d.replaceWith(box);
  });
  for (const id of ["routers", "connections"]) {
    state[id].filters.lifecycle = "all";
    if (state[id].filters.cloud) state[id].filters.cloud = "all";
    render(id);
  }
}
$("foot").textContent = "Source: Equinix Fabric v4 (connections, Cloud Routers, routes, routing protocols, statistics, Cloud Events, prices), Network Edge v1 and Billing v2 APIs, via collect_fabric_inventory.py. Amounts in " + (B ? B.currency : "USD") + ", before tax unless noted.";
</script>
"""
