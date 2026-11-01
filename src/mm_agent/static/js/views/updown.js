/* updown_window: price to beat vs live spot, countdown (a pure UI clock), Up / Down buttons at the ask. */
import { esc, num, cents, pct, paintRows, allRows, extrasHtml, compact } from "./common.js";

const usd = (n) => (Number.isFinite(n) ? "$" + n.toLocaleString("en-US", { minimumFractionDigits: n < 100 ? 4 : 2, maximumFractionDigits: n < 100 ? 4 : 2 }) : "—");
export function countdown(endTs, nowMs) {
  const s = Math.max(0, Math.floor(endTs - nowMs / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

export function render(view) {
  const c = (view.raw_extra && view.raw_extra.crypto) || {};
  const row = allRows(view)[0];
  const spot = view.spot && num(view.spot.price);
  const ptb = num(c.price_to_beat);
  const d = Number.isFinite(spot) && Number.isFinite(ptb) ? spot - ptb : NaN;
  const wins = (c.windows || []).slice(0, 4);
  const id = row ? esc(row.market_id) : "";
  return (
    '<div class="lv-upd"><div class="lv-cd"><b data-cd="' + esc(c.end_ts || "") + '">' + (c.end_ts ? countdown(c.end_ts, Date.now()) : "—") + "</b><small>left</small></div>" +
    '<div class="lv-upgrid"><div><small>Price to beat</small><b data-u="ptb">' + usd(ptb) + "</b></div>" +
    "<div><small>Current " + esc(c.symbol || "") + '</small><b data-u="cur">' + usd(spot) + '</b><small data-u="diff" class="' + (Number.isFinite(d) ? (d >= 0 ? "pos" : "neg") : "") + '">' + (Number.isFinite(d) ? (d >= 0 ? "▲ " : "▼ ") + usd(Math.abs(d)) : "") + "</small></div></div></div>" +
    (row
      ? '<div class="lv-heroes two"><button class="lv-hero home" data-trade="' + id + '" data-out="YES"><span>' + esc((row.yes || {}).label || "Up") + '</span><b data-row="' + id + '" data-f="yes_ask">' + cents((row.yes || {}).ask) + '</b><small data-row="' + id + '" data-f="chance">' + pct(row.chance) + "</small></button>" +
        '<button class="lv-hero away" data-trade="' + id + '" data-out="NO"><span>' + esc((row.no || {}).label || "Down") + '</span><b data-row="' + id + '" data-f="no_ask">' + cents((row.no || {}).ask) + "</b></button></div>"
      : "") +
    (wins.length ? '<div class="lv-chips">' + wins.map((w) => '<button class="lv-chip" data-open-event="' + esc(w.slug) + '">Next window</button>').join("") + "</div>" : "") +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol." + (view.spot ? " · spot: " + esc(view.spot.source || "") : "") + "</div>" +
    extrasHtml(view)
  );
}
export function update(root, view, changes) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
  if (changes.spot && view.spot) {
    const c = (view.raw_extra && view.raw_extra.crypto) || {};
    const spot = num(view.spot.price),
      ptb = num(c.price_to_beat);
    const cur = root.querySelector('[data-u="cur"]');
    if (cur) cur.textContent = usd(spot);
    const df = root.querySelector('[data-u="diff"]');
    if (df && Number.isFinite(ptb)) {
      const d = spot - ptb;
      df.className = d >= 0 ? "pos" : "neg";
      df.textContent = (d >= 0 ? "▲ " : "▼ ") + usd(Math.abs(d));
    }
  }
}
/** The only timer a layout owns: the countdown clock. No data is fetched here. */
export function tick(root) {
  const el = root.querySelector("[data-cd]");
  if (el && el.dataset.cd) el.textContent = countdown(Number(el.dataset.cd), Date.now());
}
