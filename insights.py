"""Turn raw Equinix API data into utilization, spend and right-sizing insights.

Pure functions only (no API calls), so they can be tested against saved JSON.
"""
import datetime
import re
from collections import defaultdict

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Spend categories, in the order they stack on the spend chart.
CATEGORIES = ["Google Cloud", "AWS", "Network Edge", "Cloud Router",
              "Internet Access", "Other"]
# Keep this much headroom above the observed peak when recommending a tier.
HEADROOM = 1.5
# A redundant pair whose combined peak stays under this is considered idle.
IDLE_MBPS = 1.0
# Invoice fields worth keeping; the rest (bank details, contacts, addresses)
# is dropped so the output is safe to share.
INVOICE_FIELDS = ["transactionId", "transactionType", "transactionDate",
                  "billingCycle", "paymentDueDate", "currencyCode",
                  "totalRecurringAmount", "totalNonRecurringAmount",
                  "totalAmount"]
LINE_FIELDS = ["transactionId", "transactionDate", "activityType",
               "productCode", "productName", "productDescription",
               "recurringStartDate", "recurringEndDate", "recurringAmount",
               "nonRecurringAmount", "adjustment", "taxAmount", "totalAmount",
               "currencyCode"]
SECRET_KEYS = re.compile(r"pass(word)?|pwd|secret|license(Key|Token)", re.I)


def scrub(obj):
    """Recursively drop credential-like fields (the NE API returns admin
    passwords for cluster nodes)."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not SECRET_KEYS.search(k)}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


# ---------------------------------------------------------------- utilization

def percentile(values, pct):
    values = sorted(v for v in values if v is not None)
    if not values:
        return 0.0
    k = (len(values) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summarize_stats(conn, stats):
    """Condense a /connections/{id}/stats response into summary numbers and an
    hourly series (max of the 5-minute averages in each hour)."""
    bu = (stats.get("stats") or {}).get("bandwidthUtilization") or {}
    result = {"uuid": conn["uuid"], "name": conn.get("name"),
              "cloud": conn.get("cloud"), "bandwidth": conn.get("bandwidth"),
              "priority": (conn.get("redundancy") or {}).get("priority"),
              "unit": bu.get("unit", "Mbps"),
              "interval": bu.get("metricInterval")}
    hours = defaultdict(lambda: [None, None])
    for i, direction in enumerate(("inbound", "outbound")):
        d = bu.get(direction) or {}
        samples = d.get("metrics") or []
        means = [m.get("mean") for m in samples]
        result[direction] = {
            "mean": d.get("mean", 0) or 0,
            "max": d.get("max", 0) or 0,
            "p95": round(percentile(means, 95), 3),
        }
        for m in samples:
            hour = (m.get("intervalEndDateTime") or "")[:13]
            if hour and m.get("mean") is not None:
                cur = hours[hour][i]
                hours[hour][i] = m["mean"] if cur is None else max(cur, m["mean"])
    keys = sorted(hours)
    result["series"] = {"hours": [k + ":00Z" for k in keys],
                        "in": [round(hours[k][0] or 0, 3) for k in keys],
                        "out": [round(hours[k][1] or 0, 3) for k in keys]}
    peak = max(result["inbound"]["max"], result["outbound"]["max"])
    p95 = max(result["inbound"]["p95"], result["outbound"]["p95"])
    bw = conn.get("bandwidth") or 0
    result["peak"] = peak
    result["p95"] = p95
    result["peak_pct"] = round(100 * peak / bw, 2) if bw else 0
    result["p95_pct"] = round(100 * p95 / bw, 2) if bw else 0
    return result


# -------------------------------------------------------------------- billing

def line_uuid(line):
    """The asset an invoice line is for. Fabric connection lines carry the
    Cloud Router's UUID in additionalInfo, but their own UUID in the product
    description, so prefer the description."""
    m = UUID_RE.search(line.get("productDescription") or "")
    if m:
        return m.group(0)
    if "Cloud Router:" not in (line.get("detailedDescription") or ""):
        m = UUID_RE.search(line.get("detailedDescription") or "")
        if m:
            return m.group(0)
    for info in line.get("additionalInfo") or []:
        if info.get("key") == "UUID" and info.get("value"):
            return info["value"]
    return None


def line_category(line):
    code = line.get("productCode") or ""
    desc = (line.get("productDescription") or "").lower()
    if code.startswith("NEDG") or "network edge" in desc:
        return "Network Edge"
    if "cloud router" in desc and "virtual connection" not in desc:
        return "Cloud Router"
    if "aws" in desc:
        return "AWS"
    if "google" in desc:
        return "Google Cloud"
    if code.startswith("EC0") or "internet access" in desc or "equinix connect" in desc:
        return "Internet Access"
    return "Other"


def line_item_name(line, names):
    uuid = line.get("uuid")
    if uuid in names:
        return names[uuid]
    desc = line.get("productDescription") or ""
    # NE lines name the node, e.g. "... -Corp-EQX-FW-Node1 -Metro ..."
    m = re.search(r"-(\S+-Node\d)\b", desc)
    if m:
        return m.group(1)
    return re.split(r"\s+-Recurring Charge", desc)[0].strip(" -")


def trim_lines(lines, names):
    out = []
    for line in lines:
        row = {k: line.get(k) for k in LINE_FIELDS}
        row["uuid"] = line_uuid(line)
        row["category"] = line_category(line)
        row["item"] = line_item_name(row, names)
        out.append(row)
    return out


def amount(line):
    return ((line.get("recurringAmount") or 0) + (line.get("nonRecurringAmount")
            or 0) + (line.get("adjustment") or 0))


def summarize_billing(invoices, lines):
    """Monthly spend by category (charges) and credits, keyed by invoice month."""
    months = defaultdict(lambda: {"charges": defaultdict(float), "credits": 0.0})
    for line in lines:
        month = (line.get("transactionDate") or "")[:7]
        value = amount(line)
        if value < 0 or "CREDIT" in (line.get("activityType") or ""):
            months[month]["credits"] += value
        else:
            months[month]["charges"][line["category"]] += value
    monthly = [{"month": m, "charges": {c: round(months[m]["charges"].get(c, 0), 2)
                                        for c in CATEGORIES},
                "credits": round(months[m]["credits"], 2)}
               for m in sorted(months)]
    for row in monthly:
        row["total_charges"] = round(sum(row["charges"].values()), 2)

    invoices = sorted(invoices, key=lambda i: i.get("transactionDate") or "")
    latest = next((i for i in reversed(invoices)
                   if i.get("transactionType") == "INVOICE"), None)
    latest_month = (latest or {}).get("transactionDate", "")[:7]
    current_items = defaultdict(lambda: {"amount": 0.0})
    for line in lines:
        if (line.get("transactionDate") or "")[:7] == latest_month and amount(line) > 0:
            key = line.get("uuid") or line["item"]
            item = current_items[key]
            item.update(item=line["item"], category=line["category"],
                        uuid=line.get("uuid"), product=line.get("productName"))
            item["amount"] += amount(line)
    last12 = [i for i in invoices if i.get("transactionDate", "") >= (
        datetime.date.fromisoformat(latest["transactionDate"]).replace(
            day=1) - datetime.timedelta(days=335)).isoformat()] if latest else []
    return {
        "currency": (latest or {}).get("currencyCode", "USD"),
        "latest_invoice": latest,
        "run_rate": (latest or {}).get("totalRecurringAmount", 0),
        "last12_total": round(sum(i.get("totalAmount") or 0 for i in last12), 2),
        "last12_credits": round(sum(m["credits"] for m in monthly[-12:]), 2),
        "monthly": monthly[-12:],
        "current_items": sorted(current_items.values(),
                                key=lambda i: -i["amount"]),
    }


# --------------------------------------------------------------- right-sizing

def price_table(prices):
    """{bandwidth Mbps: monthly list price} from a /prices/search response."""
    table = {}
    for p in prices:
        bw = (p.get("connection") or {}).get("bandwidth")
        mrc = next((c.get("price") for c in p.get("charges") or []
                    if c.get("type") == "MONTHLY_RECURRING"), None)
        if bw and mrc is not None:
            table[int(bw)] = min(mrc, table.get(int(bw), mrc))
    return dict(sorted(table.items()))


def rightsizing(utilization, prices, current_items):
    """Recommend a smaller bandwidth tier where traffic allows.

    Links are sized as redundant pairs (same cloud and bandwidth): each link
    must carry the pair's combined peak if its partner fails, plus HEADROOM.
    """
    tiers = price_table(prices)
    cost = {i.get("uuid"): i["amount"] for i in current_items if i.get("uuid")}
    groups = defaultdict(list)
    for u in utilization:
        if u.get("cloud") in ("aws", "gcp") and u.get("bandwidth"):
            groups[(u["cloud"], u["bandwidth"])].append(u)
    recs = []
    for (cloud, bw), links in sorted(groups.items()):
        combined_peak = sum(link["peak"] for link in links)
        needed = combined_peak * HEADROOM
        fits = [t for t in tiers if t >= needed]
        tier = min(fits) if fits else bw
        idle = combined_peak < IDLE_MBPS
        for link in links:
            current = cost.get(link["uuid"], tiers.get(bw, 0))
            if idle:
                action, new = "decommission", 0
            elif tier < bw:
                action, new = "downsize", tiers.get(tier, current)
            else:
                action, new = "keep", current
            recs.append({
                "action": action,
                "uuid": link["uuid"], "name": link["name"], "cloud": cloud,
                "priority": link.get("priority"), "bandwidth": bw,
                "peak": link["peak"], "combined_peak": round(combined_peak, 1),
                "needed": round(needed, 1),
                "recommended": 0 if idle else min(tier, bw),
                "current_cost": round(current, 2), "new_cost": round(new, 2),
                "savings": round(current - new, 2),
            })
    return {"headroom": HEADROOM, "idle_mbps": IDLE_MBPS, "tiers": tiers,
            "links": recs,
            "monthly_savings": round(sum(r["savings"] for r in recs), 2)}
