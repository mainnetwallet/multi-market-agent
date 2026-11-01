/* game_3way (Home / Draw / Away) and game_2way: scoreboard, hero buttons, tabs from view.sections. */
import { esc, badge, pct, cents, rowHtml, tabsHtml, activeSection, paintRows, allRows, extrasHtml, compact } from "./common.js";

function classify(view) {
  const g = view.game || {},
    rows = allRows(view).filter((r) => r.market_type === "moneyline");
  const norm = (s) => String(s || "").toLowerCase();
  const home = rows.find((r) => g.home && (norm(r.label) === norm(g.home.name) || norm(r.label) === norm(g.home.abbr)));
  const away = rows.find((r) => g.away && (norm(r.label) === norm(g.away.name) || norm(r.label) === norm(g.away.abbr)));
  const draw = rows.find((r) => /draw|tie/i.test(r.label));
  return { home: home || null, draw: draw || null, away: away || null, all: rows };
}

function whenText(view) {
  const g = view.game || {};
  if (g.ended || view.status === "ended" || view.status === "resolved") return '<span class="lv-tag">Final</span>';
  if (g.live) {
    const el = g.elapsed ? " " + esc(g.elapsed) + "'" : "";
    return '<span class="lv-livedot"></span><b>' + esc(g.period || "LIVE") + "</b>" + el;
  }
  if (view.start_time) return "<span>" + esc(new Date(view.start_time).toLocaleString("en-GB", { timeZone: "Asia/Dhaka", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })) + "</span>";
  return "";
}

function heroBtn(row, label, cls) {
  if (!row) return "";
  const id = esc(row.market_id);
  const off = row.status && row.status !== "open" ? " disabled" : "";
  return (
    '<button class="lv-hero ' + cls + '" data-trade="' + id + '" data-out="YES"' + off + '><span>' + esc(label) + '</span><b data-row="' + id + '" data-f="yes_ask">' + cents((row.yes || {}).ask) + '</b><small data-row="' + id + '" data-f="chance">' + pct(row.chance) + "</small></button>"
  );
}

function board(view) {
  const g = view.game;
  if (!g) return "";
  const side = (t, k) =>
    '<div class="lv-team">' + badge(t.name, t.logo, "lg") + '<b class="lv-tname">' + esc(t.name) + '</b><span class="lv-score" data-g="' + k + '">' + (t.score == null ? "–" : esc(t.score)) + "</span></div>";
  return '<div class="lv-board"><div class="lv-when" data-g="when">' + whenText(view) + "</div>" + '<div class="lv-teams">' + side(g.home, "home") + side(g.away, "away") + "</div></div>";
}

export function render(view, ui) {
  const g = view.game;
  const c = classify(view);
  const hero = new Set();
  let heroHtml = "";
  if (view.layout === "game_3way") {
    const h = c.home,
      d = c.draw,
      a = c.away;
    heroHtml = '<div class="lv-heroes">' + heroBtn(h, h ? h.label : "", "home") + heroBtn(d, d ? d.label : "Draw", "draw") + heroBtn(a, a ? a.label : "", "away") + "</div>";
    [h, d, a].forEach((r) => r && hero.add(String(r.market_id)));
  } else if (c.all.length) {
    const two = c.all.slice(0, 2);
    heroHtml = '<div class="lv-heroes two">' + two.map((r, i) => heroBtn(r, r.label, i ? "away" : "home")).join("") + "</div>";
    two.forEach((r) => hero.add(String(r.market_id)));
  }
  const sec = activeSection(view, ui.tab);
  const rows = sec.rows.filter((r) => !hero.has(String(r.market_id)));
  return (
    board(view) + heroHtml + tabsHtml(view, sec.id) +
    '<div class="lv-list">' + rows.map((r) => rowHtml(r, { logo: false })).join("") + "</div>" +
    '<div class="lv-meta">' + compact(view.volume, view.volume_unit) + " Vol." + (g && g.source ? " · score: " + esc(g.source) : "") + "</div>" +
    extrasHtml(view)
  );
}

export function update(root, view, changes, ui) {
  paintRows(root, new Map(allRows(view).map((r) => [String(r.market_id), r])), changes.rows || []);
  const g = view.game;
  if (g && changes.game) {
    for (const k of ["home", "away"]) {
      const el = root.querySelector('[data-g="' + k + '"]');
      if (el) el.textContent = g[k].score == null ? "–" : String(g[k].score);
    }
    const w = root.querySelector('[data-g="when"]');
    if (w) w.innerHTML = whenText(view);
  }
  if (changes.status && view.status) {
    const w = root.querySelector('[data-g="when"]');
    if (w) w.innerHTML = whenText(view);
  }
}
