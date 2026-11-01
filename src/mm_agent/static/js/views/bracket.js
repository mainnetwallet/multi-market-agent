/* bracket_ladder: ordered ranges, 24h change, Buy Yes / Buy No at the ask. Chart + legend are drawn by the shell. */
import { esc, rowHtml, paintRows, allRows, extrasHtml, compact } from "./common.js";

export function render(view) {
  const rows = allRows(view);
  return (
    '<div class="lv-list ladder">' + rows.map((r) => rowHtml(r, { sub: false })).join("") + "</div>" +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol." + (view.end_time ? " · ends " + esc(new Date(view.end_time).toLocaleDateString("en-GB", { timeZone: "Asia/Dhaka", day: "numeric", month: "short", year: "numeric" })) : "") + "</div>" +
    extrasHtml(view)
  );
}
export function update(root, view, changes) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
}
