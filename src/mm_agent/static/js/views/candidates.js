/* multi_candidates: several Yes/No outcomes, highest chance first, with logos (initials fallback). A one-line score
   shows when the event has a game block (Kalshi sports scores, polled; the line names the source). */
import { esc, rowHtml, paintRows, allRows, extrasHtml, compact } from "./common.js";

export function scoreText(view) {
  const g = view && view.game;
  if (!g || !g.home || !g.away) return "";
  const s = (t) => (t.score == null ? "–" : String(t.score));
  return (g.home.name || "Home") + " " + s(g.home) + " – " + s(g.away) + " " + (g.away.name || "Away") + (g.period ? " · " + g.period : "") + (g.source ? " · score: " + g.source : "");
}

export function render(view) {
  const rows = allRows(view).slice().sort((a, b) => Number(b.chance || 0) - Number(a.chance || 0));
  const line = scoreText(view);
  return (
    (line ? '<div class="lv-meta" data-scoreline>' + esc(line) + "</div>" : "") +
    '<div class="lv-list">' + rows.map((r) => rowHtml(r, { logo: true, sub: false })).join("") + "</div>" +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol." + "</div>" +
    extrasHtml(view)
  );
}
export function update(root, view, changes) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
  const el = changes.game ? root.querySelector("[data-scoreline]") : null;
  if (el) el.textContent = scoreText(view);
}
