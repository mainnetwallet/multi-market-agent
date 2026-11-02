/* Shared helpers for the live views. Display only: numbers here are formatting, never order inputs. */

export const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export const num = (v) => {
  if (v == null || v === "") return NaN;
  const n = Number(v);
  return Number.isFinite(n) ? n : NaN;
};

/** chance "0.555" -> "56%"; below 1% -> "<1%"; unknown -> "—". Chance is a midpoint (polymarket.com rule). */
export function pct(v) {
  const n = num(v);
  if (Number.isNaN(n)) return "—";
  const p = n * 100;
  if (p > 0 && p < 1) return "<1%";
  return Math.round(p) + "%";
}
/** ask "0.56" -> "56¢" (buttons show the ask). */
export function cents(v) {
  const n = num(v);
  if (Number.isNaN(n)) return "—";
  const c = n * 100;
  return (Math.abs(c - Math.round(c)) < 0.05 ? String(Math.round(c)) : c.toFixed(1)) + "¢";
}
/** change_1d "0.02" -> {text:"+2.0 pp", cls:"pos"}; zero/unknown -> null. */
export function change(v) {
  const n = num(v);
  if (Number.isNaN(n) || n === 0) return null;
  const pp = n * 100;
  return { text: (pp > 0 ? "+" : "") + pp.toFixed(1) + " pp", cls: pp > 0 ? "pos" : "neg" };
}
export function compact(v, unit) {
  const n = num(v);
  if (Number.isNaN(n)) return "—";
  const a = Math.abs(n);
  const s = a >= 1e9 ? (n / 1e9).toFixed(1) + "B" : a >= 1e6 ? (n / 1e6).toFixed(1) + "M" : a >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(Math.round(n));
  return (unit === "contracts" ? "" : "$") + s + (unit === "contracts" ? " contracts" : "");
}

export function initials(name) {
  const w = String(name || "?").trim().split(/\s+/).filter(Boolean);
  if (!w.length) return "?";
  return ((w[0][0] || "") + (w.length > 1 ? w[w.length - 1][0] || "" : w[0][1] || "")).toUpperCase();
}
/** A crest or logo; an initials badge when there is no usable https image (onerror swaps in the badge). */
export function badge(name, logo, cls = "") {
  const ini = esc(initials(name));
  if (logo && /^https:\/\//.test(String(logo)))
    return '<span class="lv-badge ' + cls + '" data-ini="' + ini + '"><img src="' + esc(logo) + '" alt="" loading="lazy" referrerpolicy="no-referrer"></span>';
  return '<span class="lv-badge ' + cls + '">' + ini + "</span>";
}

/** Buy Yes / Buy No buttons priced at the ask. Clicking opens the existing trade sheet pre-set to that outcome. */
export function tradeButtons(row, extra = "") {
  const id = esc(row.market_id);
  const y = row.yes || {},
    n = row.no || {};
  const closed = row.status && row.status !== "open";
  const dis = closed ? " disabled" : "";
  return (
    '<span class="lv-btns' + extra + '">' +
    '<button class="lv-btn yes" data-trade="' + id + '" data-out="YES"' + dis + ">" + esc(y.label || "Yes") + ' <b data-row="' + id + '" data-f="yes_ask">' + cents(y.ask) + "</b></button>" +
    '<button class="lv-btn no" data-trade="' + id + '" data-out="NO"' + dis + ">" + esc(n.label || "No") + ' <b data-row="' + id + '" data-f="no_ask">' + cents(n.ask) + "</b></button>" +
    "</span>"
  );
}

export function chanceCell(row) {
  return '<span class="lv-pct" data-row="' + esc(row.market_id) + '" data-f="chance">' + pct(row.chance) + "</span>";
}
export function changeCell(row) {
  const c = change(row.change_1d);
  return '<small class="lv-chg ' + (c ? c.cls : "") + '" data-row="' + esc(row.market_id) + '" data-f="change">' + (c ? esc(c.text) + " 24h" : "") + "</small>";
}

/** One generic market row (label, chance, buttons). Used by every layout for sections without a special design. */
export function rowHtml(row, opts = {}) {
  const sub = opts.sub !== false && row.sub_label && row.sub_label !== row.label ? '<small class="lv-sub">' + esc(row.sub_label) + "</small>" : "";
  const closed = row.status && row.status !== "open" ? ' <span class="lv-tag">' + esc(row.status) + "</span>" : "";
  return (
    '<div class="lv-row" data-rowbox="' + esc(row.market_id) + '">' +
    (opts.logo ? badge(row.label, row.logo) : "") +
    '<div class="lv-lab"><b>' + esc(row.label) + "</b>" + closed + sub + changeCell(row) + "</div>" +
    chanceCell(row) + tradeButtons(row) + "</div>"
  );
}

/** Targeted DOM update after a patch: only text of cells bound with data-row / data-f changes. */
export function paintRows(root, rowsById, ids) {
  for (const id of ids) {
    const row = rowsById.get(String(id));
    if (!row) continue;
    const sel = '[data-row="' + (window.CSS && CSS.escape ? CSS.escape(String(id)) : String(id).replace(/"/g, '\\"')) + '"]';
    for (const el of root.querySelectorAll(sel)) {
      const f = el.dataset.f;
      if (f === "chance") el.textContent = pct(row.chance);
      else if (f === "yes_ask") el.textContent = cents((row.yes || {}).ask);
      else if (f === "no_ask") el.textContent = cents((row.no || {}).ask);
      else if (f === "change") {
        const c = change(row.change_1d);
        el.className = "lv-chg " + (c ? c.cls : "");
        el.textContent = c ? c.text + " 24h" : "";
      }
    }
    const box = root.querySelector('[data-rowbox="' + String(id).replace(/"/g, '\\"') + '"]');
    if (box) for (const b of box.querySelectorAll("button[data-trade]")) b.disabled = !!(row.status && row.status !== "open");
  }
}

export function allRows(view) {
  const out = [];
  for (const s of view.sections || []) for (const r of s.rows || []) out.push(r);
  return out;
}

/** Sibling chips and related events, shown under every layout when the server filled them. */
export function extrasHtml(view) {
  let h = "";
  if ((view.chips || []).length)
    h += '<div class="lv-chips">' + view.chips.map((c) => '<button class="lv-chip" data-open-event="' + esc(c.event_ref) + '">' + esc(c.label) + "</button>").join("") + "</div>";
  if ((view.related || []).length)
    h +=
      '<div class="lv-related"><h4>Related</h4>' +
      view.related
        .map((r) => '<button class="lv-rel" data-open-event="' + esc(r.event_ref) + '">' + (r.image ? badge(r.title, r.image) : "") + "<span>" + esc(r.title) + "</span><b>" + pct(r.chance) + "</b></button>")
        .join("") +
      "</div>";
  return h;
}

/** Section tabs. Never shows an empty tab. */
export function tabsHtml(view, active) {
  const secs = (view.sections || []).filter((s) => (s.rows || []).length);
  if (secs.length < 2) return "";
  return '<div class="lv-tabs" role="tablist">' + secs.map((s) => '<button role="tab" class="lv-tab' + (s.id === active ? " on" : "") + '" data-tab="' + esc(s.id) + '">' + esc(s.label) + "</button>").join("") + "</div>";
}
export function activeSection(view, want) {
  const secs = (view.sections || []).filter((s) => (s.rows || []).length);
  return secs.find((s) => s.id === want) || secs[0] || { id: "", label: "", rows: [] };
}
