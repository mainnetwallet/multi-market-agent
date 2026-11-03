/* Data tab of the Markets view: a form and a result renderer driven by GET /v1/polymarket/catalog.
   Read-only. A new backend route shows up here with no UI change. */
import { esc, compact } from "./views/common.js";

const CURSOR_PARAM = { cursor: "cursor", keyset: "after_cursor", clob: "next_cursor" };

/** Query string pairs for one route from the form values. Empty values are dropped. Pure. */
export function buildQuery(route, values) {
  const q = new URLSearchParams();
  for (const p of route.params || []) {
    const v = values[p.name];
    if (v !== undefined && v !== null && String(v).trim() !== "") q.set(p.name, String(v).trim());
  }
  return q;
}

/** Names the form still has to fill in before Run makes sense. Pure. */
export function missing(route, values) {
  const has = (n) => String(values[n] ?? "").trim() !== "";
  const out = (route.path_params || []).filter((n) => !has(n));
  if ((route.needs_any || []).length && !route.needs_any.some(has)) out.push(route.needs_any.join(" or "));
  if ((route.needs_one || []).length && route.needs_one.filter(has).length !== 1) out.push("exactly one of " + route.needs_one.join(", "));
  return out;
}

/** The URL the UI calls. Path parameters of the old Data API are plain query parameters, so one shape fits all. */
export function exploreUrl(route, values) {
  const q = buildQuery(route, values).toString();
  return "/v1/polymarket/explore/" + encodeURIComponent(route.key) + (q ? "?" + q : "");
}

export function accountUrl(route, values) {
  const q = buildQuery(route, values).toString();
  return "/v1/polymarket/account/" + encodeURIComponent(route.key.replace(/^account\./, "")) + (q ? "?" + q : "");
}

/** Pull the list of row objects out of any answer shape the venue uses. Pure. */
export function rowsOf(result) {
  if (Array.isArray(result)) return result;
  if (!result || typeof result !== "object") return null;
  for (const k of ["data", "items", "markets", "events", "comments", "tags", "results"]) {
    if (Array.isArray(result[k])) return result[k];
  }
  const arrays = Object.values(result).filter((v) => Array.isArray(v));
  return arrays.length === 1 ? arrays[0] : null;
}

/** Next-page request, or null when there is no more. Pure. */
export function nextPage(route, result, values) {
  if (!result || typeof result !== "object") return null;
  const cursorName = CURSOR_PARAM[route.paging === "cursor" && route.host === "clob" ? "clob" : route.paging];
  const pag = result.pagination || {};
  const cur = pag.next_cursor ?? result.next_cursor ?? null;
  if (cursorName && cur && cur !== "LTE=" && (pag.has_more ?? true)) return { ...values, [cursorName]: cur };
  if (route.paging === "offset") {
    const rows = rowsOf(result) || [];
    const limit = Number(values.limit || 100);
    if (rows.length >= limit) return { ...values, offset: String(Number(values.offset || 0) + limit) };
  }
  return null;
}

/** [t, value] points for a sparkline from the series shapes the venue sends. Pure. */
export function seriesPoints(result) {
  const rows = rowsOf(result) || (result && Array.isArray(result.history) ? result.history : null);
  if (!rows) return [];
  const pts = [];
  for (const r of rows) {
    if (Array.isArray(r)) pts.push([Number(r[0]), Number(r[1])]);
    else if (r && typeof r === "object") {
      const t = r.t ?? r.timestamp ?? r.time ?? r.date;
      const v = r.p ?? r.price ?? r.value ?? r.pnl ?? r.volume ?? r.v;
      if (t !== undefined && v !== undefined) pts.push([typeof t === "string" && Number.isNaN(Number(t)) ? Date.parse(t) : Number(t), Number(v)]);
    }
  }
  return pts.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
}

export function sparkline(points, w = 320, h = 70) {
  if (points.length < 2) return '<div class="muted tiny">Not enough points for a chart.</div>';
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const sx = (x) => ((x - x0) / (x1 - x0 || 1)) * (w - 4) + 2;
  const sy = (y) => h - 4 - ((y - y0) / (y1 - y0 || 1)) * (h - 8);
  const d = points.map((p, i) => (i ? "L" : "M") + sx(p[0]).toFixed(1) + " " + sy(p[1]).toFixed(1)).join(" ");
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;max-width:${w}px;height:${h}px"><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2"/></svg><div class="tiny muted">min ${esc(compact(y0))} · max ${esc(compact(y1))} · ${points.length} points</div>`;
}

function cell(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return esc(JSON.stringify(v).slice(0, 80));
  const s = String(v);
  return esc(s.length > 60 ? s.slice(0, 57) + "…" : s);
}

export function tableHtml(rows, limit = 100) {
  if (!rows.length) return '<div class="muted">No rows.</div>';
  const objects = rows.filter((r) => r && typeof r === "object" && !Array.isArray(r));
  if (!objects.length) return `<pre class="tiny">${esc(JSON.stringify(rows.slice(0, limit), null, 1))}</pre>`;
  const cols = [];
  for (const r of objects.slice(0, 30)) for (const k of Object.keys(r)) if (!cols.includes(k) && cols.length < 8) cols.push(k);
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = objects
    .slice(0, limit)
    .map((r) => "<tr>" + cols.map((c) => `<td>${cell(r[c])}</td>`).join("") + "</tr>")
    .join("");
  return `<div style="overflow-x:auto"><table class="xp-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div><div class="tiny muted">${Math.min(rows.length, limit)} of ${rows.length} rows shown</div>`;
}

export function objectHtml(obj) {
  if (obj === null || typeof obj !== "object") return `<div>${cell(obj)}</div>`;
  const rows = Object.entries(obj)
    .map(([k, v]) => `<div class="xp-kv"><span class="muted">${esc(k)}</span><b>${cell(v)}</b></div>`)
    .join("");
  return `<div class="xp-obj">${rows || '<span class="muted">Empty.</span>'}</div>`;
}

/** Render one answer by the route's result kind, falling back to what the data looks like. Pure (returns HTML). */
export function renderResult(route, result) {
  const body = result && typeof result === "object" && "result" in result && result.ok === true ? result.result : result;
  if (route.result === "text" || typeof body === "string" || typeof body === "number") return `<pre class="tiny">${esc(String(body))}</pre>`;
  if (route.result === "series") {
    const pts = seriesPoints(body);
    if (pts.length > 1) return sparkline(pts);
  }
  const rows = rowsOf(body);
  if (rows && route.result !== "object") return tableHtml(rows);
  if (rows && rows.length > 1) return tableHtml(rows);
  return objectHtml(body);
}

const CSS = `.xp-table{border-collapse:collapse;font-size:12px;width:100%}.xp-table th,.xp-table td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap}.xp-table th{color:var(--muted);font-weight:600}
.xp-kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--line);font-size:13px}.xp-kv b{text-align:right;word-break:break-all;font-weight:600}
.xp-form{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}.xp-form label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--muted)}
.xp-badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:999px;border:1px solid var(--line2);color:var(--muted);margin-left:6px}.xp-badge.warn{color:var(--warn);border-color:var(--warn)}`;

export function mountExplorer(root, opts = {}) {
  const fetchFn = opts.fetch || ((u) => fetch(u, { credentials: "same-origin" }));
  const state = { cat: null, group: "", key: "", values: {}, last: null };
  if (!root.querySelector("style[data-xp]")) {
    const st = document.createElement("style");
    st.dataset.xp = "1";
    st.textContent = CSS;
    root.appendChild(st);
  }
  const body = document.createElement("div");
  root.appendChild(body);

  async function getJson(url) {
    const r = await fetchFn(url);
    let j = {};
    try {
      j = await r.json();
    } catch (e) {}
    if (!r.ok) {
      const err = new Error(typeof j.detail === "string" ? j.detail : "HTTP " + r.status);
      err.retryAfter = r.headers && r.headers.get ? r.headers.get("Retry-After") : null;
      throw err;
    }
    return j;
  }

  const routesOf = (g) => (state.cat ? state.cat.routes.filter((r) => r.group === g) : []);
  const routeNow = () => (state.cat ? state.cat.routes.find((r) => r.key === state.key) : null);

  function draw() {
    if (!state.cat) {
      body.innerHTML = '<div class="muted">Loading catalog…</div>';
      return;
    }
    const groups = state.cat.groups.filter((g) => routesOf(g.id).length);
    const tabs = groups.map((g) => `<button class="tab${g.id === state.group ? " on" : ""}" data-g="${esc(g.id)}">${esc(g.label)}</button>`).join("");
    const rs = routesOf(state.group);
    const opts2 = rs.map((r) => `<option value="${esc(r.key)}"${r.key === state.key ? " selected" : ""}>${esc(r.label)}</option>`).join("");
    const route = routeNow();
    let form = "";
    if (route) {
      const fields = route.params
        .map((p) => `<label>${esc(p.name)}<input data-p="${esc(p.name)}" value="${esc(state.values[p.name] || "")}" placeholder="${esc(p.hint || p.type)}" autocomplete="off"></label>`)
        .join("");
      const badges =
        (route.evidence === "U" ? '<span class="xp-badge warn">path not verified</span>' : "") +
        (route.class === "R1" ? '<span class="xp-badge">needs credentials</span>' : "") +
        (route.rate_limit ? `<span class="xp-badge">${route.rate_limit}/10s</span>` : "");
      form = `<div class="tiny muted" style="margin-top:6px">${esc(route.method)} ${esc(route.path || "(account)")}${badges}</div>${route.note ? `<div class="tiny muted">${esc(route.note)}</div>` : ""}<div class="xp-form">${fields}</div><div class="row"><button class="btn primary small" id="xpRun">${route.result === "file" ? "Download" : "Run"}</button><button class="btn small hidden" id="xpNext">Next page</button></div>`;
    }
    body.innerHTML = `<div class="tabbar sub">${tabs}</div><select id="xpRoute" style="width:100%;margin-top:6px">${opts2}</select>${form}<div id="xpOut" style="margin-top:10px"></div>`;
    body.querySelectorAll("[data-g]").forEach((b) => b.addEventListener("click", () => selectGroup(b.dataset.g)));
    const sel = body.querySelector("#xpRoute");
    if (sel) sel.addEventListener("change", () => ((state.key = sel.value), (state.values = {}), draw(), autoRun()));
    body.querySelectorAll("[data-p]").forEach((i) => i.addEventListener("input", () => (state.values[i.dataset.p] = i.value)));
    const run = body.querySelector("#xpRun");
    if (run) run.addEventListener("click", () => exec(state.values));
    const next = body.querySelector("#xpNext");
    if (next) next.addEventListener("click", () => state.last && exec(state.last));
  }

  function out(html) {
    const el = body.querySelector("#xpOut");
    if (el) el.innerHTML = html;
  }

  async function exec(values) {
    const route = routeNow();
    if (!route) return;
    state.values = { ...values };
    const lack = missing(route, state.values);
    if (lack.length) return out(`<div class="muted">Fill in: ${esc(lack.join(", "))}</div>`);
    if (route.result === "file") {
      window.open(exploreUrl(route, state.values), "_blank", "noopener");
      return out('<div class="muted">Download started.</div>');
    }
    out('<div class="muted">Loading…</div>');
    try {
      const url = route.class === "R1" ? accountUrl(route, state.values) : exploreUrl(route, state.values);
      const j = await getJson(url);
      if (j.needs_credentials) return out('<div class="muted">This view needs Polymarket credentials. They are not set for this account.</div>');
      out(renderResult(route, j));
      const nxt = nextPage(route, j.result ?? j, state.values);
      const btn = body.querySelector("#xpNext");
      if (btn) btn.classList.toggle("hidden", !nxt);
      state.last = nxt;
    } catch (e) {
      const wait = e.retryAfter ? ` Try again in ${esc(String(e.retryAfter))}s.` : "";
      out(`<div style="color:var(--danger)">${esc(e.message)}${wait}</div>`);
    }
  }

  async function systemTile() {
    out('<div class="muted">Checking…</div>');
    try {
      const j = await getJson("/v1/polymarket/system");
      const rows = Object.entries(j.checks).map(([k, v]) => {
        const val = v.value !== undefined ? (typeof v.value === "object" ? JSON.stringify(v.value).slice(0, 60) : String(v.value).slice(0, 60)) : v.detail || (v.skew_seconds !== undefined ? v.skew_seconds + "s skew" : "");
        return `<div class="xp-kv"><span>${esc(k)}</span><b style="color:var(${v.ok ? "--accent" : "--danger"})">${v.ok ? "ok" : "problem"} ${esc(val)}</b></div>`;
      });
      let writes = "";
      try {
        writes = writeStatusHtml(await getJson("/v1/polymarket/write"));
      } catch (e) {
        writes = "";
      }
      out(`<div class="xp-obj">${rows.join("")}</div>${writes}`);
    } catch (e) {
      out(`<div style="color:var(--danger)">${esc(e.message)}</div>`);
    }
  }

  function autoRun() {
    if (state.group === "system" && state.key === "") return systemTile();
  }

  function selectGroup(g) {
    state.group = g;
    state.key = (routesOf(g)[0] || {}).key || "";
    state.values = {};
    draw();
    if (g === "system") systemTile();
  }

  async function load() {
    draw();
    try {
      state.cat = await getJson("/v1/polymarket/catalog");
      const first = state.cat.groups.find((g) => routesOf(g.id).length);
      selectGroup(first ? first.id : "");
    } catch (e) {
      body.innerHTML = `<div style="color:var(--danger)">${esc(e.message)}</div>`;
    }
  }

  load();
  return { reload: load };
}

/** Read-only status of the write-class API. There are no buttons here on purpose: it is a status list. */
export function writeStatusHtml(j) {
  if (!j || !Array.isArray(j.actions)) return "";
  const rows = j.actions.map(
    (a) =>
      `<div class="xp-kv"><span>${esc(a.key)}</span><b style="color:var(${a.enabled ? "--accent" : "--muted"})">${a.enabled ? "enabled" : "off"}${a.ack ? " · typed text" : ""}</b></div>`,
  );
  const hb = j.heartbeat || {};
  const head = `<div class="muted tiny" style="margin-top:12px">Write API ${j.enabled ? "on" : "off"} (env flags, step-up and Idempotency-Key needed). Heartbeat ${hb.running ? "running" : "off"}.</div>`;
  return head + `<div class="xp-obj">${rows.join("")}</div>`;
}

/** Collapsible "Market insights" for the market sheet: holders, open interest, resolution, fee, tick, CLOB info. */
export async function mountMarketInsights(el, market, opts = {}) {
  const fetchFn = opts.fetch || ((u) => fetch(u, { credentials: "same-origin" }));
  if (!market || String(market.venue) !== "polymarket" || !/^0x[0-9a-fA-F]{64}$/.test(String(market.native_id || ""))) {
    el.innerHTML = '<div class="muted tiny">Insights are available for Polymarket markets.</div>';
    return;
  }
  const cond = String(market.native_id).toLowerCase();
  const token = (market.outcomes && market.outcomes[0] && market.outcomes[0].native_id) || "";
  const get = async (key, q) => {
    try {
      const r = await fetchFn("/v1/polymarket/explore/" + key + "?" + new URLSearchParams(q).toString());
      const j = await r.json();
      return r.ok ? j.result : { error: typeof j.detail === "string" ? j.detail : "HTTP " + r.status };
    } catch (e) {
      return { error: String(e.message || e) };
    }
  };
  el.innerHTML = '<div class="muted tiny">Loading insights…</div>';
  const [holders, oi, res, info, fee, tick] = await Promise.all([
    get("data.holders", { condition: cond, include_pnl: "true", limit: "10" }),
    get("data.oi", { condition: cond }),
    get("data.resolutions", { condition: cond }),
    get("clob.market.info", { condition_id: cond }),
    token ? get("clob.fee_rate", { token_id: token }) : { error: "no token" },
    token ? get("clob.tick_size", { token_id: token }) : { error: "no token" },
  ]);
  const block = (title, v, kind) => `<h4 style="margin:12px 0 4px">${esc(title)}</h4>` + (v && v.error ? `<div class="tiny muted">${esc(v.error)}</div>` : renderResult({ result: kind }, v));
  el.innerHTML =
    block("Top holders (with PnL)", holders, "table") +
    block("Open interest", oi, "object") +
    block("Resolution", res, "object") +
    block("Fee rate", fee, "object") +
    block("Tick size", tick, "object") +
    block("CLOB market info", info, "object");
}
