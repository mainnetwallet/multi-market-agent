/* multi_candidates: several Yes/No outcomes, highest chance first, with logos (initials fallback). */
import { esc, rowHtml, paintRows, allRows, extrasHtml, compact } from "./common.js";

export function render(view) {
  const rows = allRows(view).slice().sort((a, b) => Number(b.chance || 0) - Number(a.chance || 0));
  return (
    '<div class="lv-list">' + rows.map((r) => rowHtml(r, { logo: true, sub: false })).join("") + "</div>" +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol." + "</div>" +
    extrasHtml(view)
  );
}
export function update(root, view, changes) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
}
