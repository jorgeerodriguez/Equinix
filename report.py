"""Render the Fabric / Network Edge inventory as a self-contained HTML report."""
import datetime
import json


def get(obj, path, default=""):
    """Fetch a nested value by '/'-separated path, e.g. 'aSide/accessPoint'."""
    for key in path.split("/"):
        obj = obj.get(key) if isinstance(obj, dict) else None
    return default if obj is None else obj


def endpoint(side):
    ap = get(side, "accessPoint", {})
    name = (get(ap, "router/name") or get(ap, "virtualDevice/name")
            or get(ap, "profile/name") or get(ap, "port/name")
            or get(ap, "network/name"))
    return {"name": name or ap.get("type", ""), "kind": ap.get("type", "")}


def connection_row(c, lifecycle):
    return {
        "lifecycle": lifecycle,
        "name": c.get("name"),
        "status": get(c, "operation/equinixStatus"),
        "provider": get(c, "operation/providerStatus"),
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
    return {
        "name": d.get("name"),
        "status": d.get("status"),
        "type": d.get("deviceTypeName") or d.get("deviceTypeCode"),
        "vendor": d.get("deviceTypeVendor"),
        "metro": d.get("metroCode"),
        "metroName": d.get("metroName"),
        "ibx": d.get("ibx"),
        "created": str(d.get("createdDate") or "")[:10],
    }


def build_data(inventory):
    conns, routers = inventory["connections"], inventory["cloud_routers"]
    return {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "account": next((get(r, "account/accountName") for state in routers
                         for r in routers[state]), ""),
        "ports": len(inventory["ports"]),
        "connections": [connection_row(c, s) for s in ("active", "deprovisioned")
                        for c in conns[s]],
        "routers": [router_row(r, s) for s in ("active", "deprovisioned")
                    for r in routers[s]],
        "devices": [device_row(d) for d in inventory["network_edge_devices"]],
    }


def render_html(inventory):
    data = json.dumps(build_data(inventory), default=str).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data)


TEMPLATE = r"""<title>Equinix Fabric Inventory</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
/* Layout: summary strip, then topology-ordered sections (edge -> router -> circuits), tables scroll inside their own frames. */
:root {
  --bg: #f5f7f8; --surface: #ffffff; --fg: #17212b; --muted: #5d6b78; --line: #dde3e8;
  --accent: #0b6e85; --accent-soft: #e2f1f4;
  --ok: #1f7a4a; --ok-soft: #e3f3ea; --off: #6b7580; --off-soft: #eceff2;
  --warn: #9a5b00; --warn-soft: #fbf0dc;
  --aws: #b35c00; --aws-soft: #fdeedd; --gcp: #2457c5; --gcp-soft: #e4ecfb;
  --inet: #6b46b8; --inet-soft: #efe9fa; --other: #48606f; --other-soft: #e7edf0;
  --display: "IBM Plex Sans Condensed", "Arial Narrow", sans-serif;
  --body: "IBM Plex Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f161c; --surface: #162029; --fg: #e4eaef; --muted: #93a2ae; --line: #26333e;
  --accent: #4fc0d8; --accent-soft: #143440;
  --ok: #5fcf8f; --ok-soft: #15332a; --off: #8d98a2; --off-soft: #222c35;
  --warn: #f0b45a; --warn-soft: #3a2b12;
  --aws: #ffad5c; --aws-soft: #3b2814; --gcp: #82a8ff; --gcp-soft: #1b2a4a;
  --inet: #b79cf2; --inet-soft: #2b2342; --other: #a9bcc8; --other-soft: #22303a;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0f161c; --surface: #162029; --fg: #e4eaef; --muted: #93a2ae; --line: #26333e;
  --accent: #4fc0d8; --accent-soft: #143440;
  --ok: #5fcf8f; --ok-soft: #15332a; --off: #8d98a2; --off-soft: #222c35;
  --warn: #f0b45a; --warn-soft: #3a2b12;
  --aws: #ffad5c; --aws-soft: #3b2814; --gcp: #82a8ff; --gcp-soft: #1b2a4a;
  --inet: #b79cf2; --inet-soft: #2b2342; --other: #a9bcc8; --other-soft: #22303a;
  color-scheme: dark; }

* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 14px;
  line-height: 1.5; padding-inline: 16px; padding-block: 28px 48px; }
.wrap { max-width: 1240px; margin-inline: auto; display: grid; gap: 28px; }
header { display: flex; flex-wrap: wrap; align-items: end; justify-content: space-between; gap: 12px; }
h1 { font-family: var(--display); font-weight: 600; font-size: 30px; line-height: 1.1;
  margin: 0; text-wrap: balance; letter-spacing: -0.01em; }
.meta { color: var(--muted); font-size: 13px; display: flex; flex-wrap: wrap; gap: 4px 16px; }
.meta b { color: var(--fg); font-weight: 500; }
h2 { font-family: var(--display); font-weight: 600; font-size: 19px; margin: 0; }
.eyebrow { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }

.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 1px;
  background: var(--line); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.stat { background: var(--surface); padding: 14px 16px; display: grid; gap: 2px; min-width: 0; }
.stat .num { font-family: var(--display); font-size: 28px; font-weight: 600; line-height: 1.15;
  font-variant-numeric: tabular-nums; }
.stat .num small { font-size: 15px; color: var(--muted); font-weight: 500; margin-left: 2px; }
.stat .sub { color: var(--muted); font-size: 12.5px; }

section { display: grid; gap: 12px; min-width: 0; }
.sec-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; }
.sec-title { display: flex; align-items: baseline; gap: 10px; }
.count { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.controls { display: flex; flex-wrap: wrap; gap: 6px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; background: var(--surface); }
.seg button { font: inherit; font-size: 12.5px; border: 0; background: transparent; color: var(--muted);
  padding: 5px 11px; cursor: pointer; }
.seg button + button { border-left: 1px solid var(--line); }
.seg button[aria-pressed="true"] { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.seg button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
input[type=search] { font: inherit; font-size: 13px; padding: 5px 10px; border: 1px solid var(--line);
  border-radius: 7px; background: var(--surface); color: var(--fg); min-width: 0; width: 200px; max-width: 100%; }

.frame { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
  font-weight: 600; padding: 9px 12px; border-bottom: 1px solid var(--line); white-space: nowrap;
  cursor: pointer; user-select: none; }
th[aria-sort] { color: var(--accent); }
th[aria-sort="ascending"]::after { content: " ▲"; font-size: 9px; }
th[aria-sort="descending"]::after { content: " ▼"; font-size: 9px; }
td { padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: color-mix(in srgb, var(--accent-soft) 45%, transparent); }
tr.gone td { color: var(--muted); }
tr.gone td.name { text-decoration: line-through; text-decoration-color: var(--off); }
.name { font-weight: 600; }
.mono, td.num, td.date { font-family: var(--mono); font-size: 12.5px; font-variant-numeric: tabular-nums; }
td.num { text-align: right; }
th.num { text-align: right; }
.ep { display: grid; line-height: 1.3; }
.ep small { font-family: var(--mono); font-size: 10.5px; color: var(--muted); letter-spacing: 0.03em; }
.arrow { color: var(--muted); padding-inline: 0; }

.pill { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600;
  padding: 2px 8px; border-radius: 999px; letter-spacing: 0.02em; }
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--ok); background: var(--ok-soft); }
.pill.off { color: var(--off); background: var(--off-soft); }
.pill.warn { color: var(--warn); background: var(--warn-soft); }
.tag { display: inline-block; font-family: var(--mono); font-size: 11px; font-weight: 500; padding: 1px 7px;
  border-radius: 4px; text-transform: uppercase; }
.tag.aws { color: var(--aws); background: var(--aws-soft); }
.tag.gcp { color: var(--gcp); background: var(--gcp-soft); }
.tag.internet { color: var(--inet); background: var(--inet-soft); }
.tag.azure, .tag.oracle, .tag.other { color: var(--other); background: var(--other-soft); }
.prio { font-family: var(--mono); font-size: 11px; color: var(--muted); }
.empty { padding: 18px; color: var(--muted); }
footer { color: var(--muted); font-size: 12px; }
@media (max-width: 560px) { h1 { font-size: 24px; } input[type=search] { width: 100%; } }
</style>

<div class="wrap">
  <header>
    <div>
      <div class="eyebrow">Equinix Fabric &amp; Network Edge</div>
      <h1>Fabric Inventory</h1>
    </div>
    <div class="meta" id="meta"></div>
  </header>

  <div class="summary" id="summary"></div>

  <section>
    <div class="sec-head">
      <div class="sec-title"><h2>Network Edge devices</h2><span class="count" id="dev-count"></span></div>
    </div>
    <div class="frame"><table id="devices"></table></div>
  </section>

  <section>
    <div class="sec-head">
      <div class="sec-title"><h2>Cloud Routers</h2><span class="count" id="rtr-count"></span></div>
      <div class="controls"><div class="seg" data-target="routers" data-key="lifecycle"></div></div>
    </div>
    <div class="frame"><table id="routers"></table></div>
  </section>

  <section>
    <div class="sec-head">
      <div class="sec-title"><h2>Connections</h2><span class="count" id="conn-count"></span></div>
      <div class="controls">
        <input type="search" id="conn-search" placeholder="Filter by name or endpoint" aria-label="Filter connections">
        <div class="seg" data-target="connections" data-key="cloud"></div>
        <div class="seg" data-target="connections" data-key="lifecycle"></div>
      </div>
    </div>
    <div class="frame"><table id="connections"></table></div>
  </section>

  <footer id="foot"></footer>
</div>

<script>
const DATA = __DATA__;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const bw = m => !m ? "—" : m >= 1000 ? (m / 1000).toLocaleString() + " Gbps" : m + " Mbps";
const GOOD = new Set(["PROVISIONED", "ACTIVE", "AVAILABLE"]);
const GONE = new Set(["DEPROVISIONED", "DELETED", "NOT_PROVISIONED"]);
const pill = s => `<span class="pill ${GOOD.has(s) ? "ok" : GONE.has(s) ? "off" : "warn"}">${esc(s || "UNKNOWN")}</span>`;
const ep = e => `<span class="ep">${esc(e.name)}<small>${esc(e.kind.replace("_", " "))}</small></span>`;
const KIND = {VD: "Edge device", CLOUD_ROUTER: "Cloud router", SP: "Service profile", COLO: "Port", NETWORK: "Network"};
const conns = DATA.connections.map(c => ({...c, a: {...c.a, kind: KIND[c.a.kind] || c.a.kind}, z: {...c.z, kind: KIND[c.z.kind] || c.z.kind}}));

const active = conns.filter(c => c.lifecycle === "active");
const sumBw = cloud => active.filter(c => c.cloud === cloud).reduce((t, c) => t + c.mbps, 0);
const count = (rows, k, v) => rows.filter(r => r[k] === v).length;

document.getElementById("meta").innerHTML =
  (DATA.account ? `<span>Account <b>${esc(DATA.account)}</b></span>` : "") +
  `<span>Generated <b>${esc(DATA.generated)}</b></span>`;
document.getElementById("summary").innerHTML = [
  ["Active connections", active.length, `${count(conns, "lifecycle", "deprovisioned")} deprovisioned`],
  ["AWS Direct Connect", bw(sumBw("aws")), `${count(active, "cloud", "aws")} active links`],
  ["Google Interconnect", bw(sumBw("gcp")), `${count(active, "cloud", "gcp")} active links`],
  ["Internet Access", bw(sumBw("internet")), `${count(active, "cloud", "internet")} active links`],
  ["Cloud Routers", count(DATA.routers, "lifecycle", "active"), `${count(DATA.routers, "lifecycle", "deprovisioned")} deprovisioned`],
  ["Network Edge", DATA.devices.length, `${DATA.ports} dedicated ports`],
].map(([label, num, sub]) => {
  const [n, unit] = String(num).split(" ");
  return `<div class="stat"><span class="eyebrow">${label}</span><span class="num">${esc(n)}${unit ? `<small>${unit}</small>` : ""}</span><span class="sub">${sub}</span></div>`;
}).join("");
document.getElementById("foot").textContent =
  `Generated by collect_fabric_inventory.py from the Equinix Fabric v4 and Network Edge v1 APIs.`;

// Table definitions: [header, key for sorting, cell renderer, css class]
const TABLES = {
  devices: { rows: DATA.devices, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["Status", "status", r => pill(r.status)],
    ["Type", "type", r => esc(r.type)],
    ["Vendor", "vendor", r => esc(r.vendor)],
    ["Metro", "metro", r => `<span class="mono">${esc(r.metro)}</span> ${esc(r.metroName)}`],
    ["IBX", "ibx", r => esc(r.ibx), "mono"],
    ["Created", "created", r => esc(r.created), "date"],
  ]},
  routers: { rows: DATA.routers, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["State", "state", r => pill(r.state)],
    ["Package", "package", r => esc(r.package), "mono"],
    ["Metro", "metro", r => `<span class="mono">${esc(r.metro)}</span> ${esc(r.metroName)}`],
    ["ASN", "asn", r => esc(r.asn), "num"],
    ["Conns", "conns", r => esc(r.conns), "num"],
    ["Created", "created", r => esc(r.created), "date"],
    ["Updated / deleted", r => r.deleted || r.updated, r => esc(r.deleted || r.updated), "date"],
  ]},
  connections: { rows: conns, cols: [
    ["Name", "name", r => esc(r.name), "name"],
    ["Status", "status", r => pill(r.status)],
    ["Cloud", "cloud", r => `<span class="tag ${esc(r.cloud)}">${esc(r.cloud)}</span>`],
    ["Bandwidth", "mbps", r => bw(r.mbps), "num"],
    ["Role", "priority", r => `<span class="prio">${esc(r.priority || "—")}</span>`],
    ["A-side", r => r.a.name, r => ep(r.a)],
    ["", null, () => "→", "arrow"],
    ["Z-side", r => r.z.name, r => ep(r.z)],
    ["Type", "type", r => esc(r.type), "mono"],
    ["Metro", "metro", r => esc(r.metro), "mono"],
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
    `<th class="${cls || ""}" ${key === null ? "" : `data-i="${i}" tabindex="0"`} ${st.sort === i ? `aria-sort="${st.dir > 0 ? "ascending" : "descending"}"` : ""}>${h}</th>`).join("") + "</tr></thead>";
  const body = rows.length
    ? rows.map(r => `<tr class="${r.lifecycle === "deprovisioned" ? "gone" : ""}">` +
        def.cols.map(([, , cell, cls]) => `<td class="${cls || ""}">${cell(r)}</td>`).join("") + "</tr>").join("")
    : `<tr><td class="empty" colspan="${def.cols.length}">No ${id} match these filters.</td></tr>`;
  const table = document.getElementById(id);
  table.innerHTML = head + "<tbody>" + body + "</tbody>";
  table.querySelectorAll("th[data-i]").forEach(th => {
    const go = () => { const i = +th.dataset.i; st.dir = st.sort === i ? -st.dir : 1; st.sort = i; render(id); };
    th.onclick = go;
    th.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  });
  const total = def.rows.length;
  const el = document.getElementById({devices: "dev-count", routers: "rtr-count", connections: "conn-count"}[id]);
  el.textContent = rows.length === total ? `${total}` : `${rows.length} of ${total}`;
}

function segment(el) {
  const id = el.dataset.target, key = el.dataset.key, rows = TABLES[id].rows;
  const opts = key === "lifecycle"
    ? [["active", "Active"], ["deprovisioned", "Deprovisioned"], ["all", "All"]]
    : [["all", "All clouds"], ...[...new Set(rows.map(r => r[key]))].sort().map(v => [v, v.toUpperCase()])];
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
document.getElementById("conn-search").oninput = e => { state.connections.q = e.target.value.trim(); render("connections"); };
Object.keys(TABLES).forEach(render);
</script>
"""
