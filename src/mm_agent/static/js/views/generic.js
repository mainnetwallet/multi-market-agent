/* generic_list and binary: any shape renders. Tabs when there are several sections. */
import { esc, rowHtml, tabsHtml, activeSection, paintRows, allRows, extrasHtml, compact, pct } from "./common.js";

export function render(view, ui) {
  const sec = activeSection(view, ui.tab);
  const big =
    view.layout === "binary" && sec.rows[0]
      ? '<div class="lv-bigchance"><b data-row="' + esc(sec.rows[0].market_id) + '" data-f="chance">' + pct(sec.rows[0].chance) + "</b> chance</div>"
      : "";
  return (
    big + tabsHtml(view, sec.id) +
    '<div class="lv-list">' + sec.rows.map((r) => rowHtml(r, { logo: view.layout === "generic_list" })).join("") + "</div>" +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol.</div>" +
    extrasHtml(view)
  );
}
export function update(root, view, changes) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
}
