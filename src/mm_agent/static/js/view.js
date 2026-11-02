/* The live event view shell: header, LIVE / DELAYED / OFFLINE indicator, layout body (from EventView), chart.
   Every layout renders from the EventView delivered by /v1/stream (REST only as a fallback inside LiveClient). */
import { liveState } from "./live.js";
import { renderChart } from "./chart.js";
import { esc } from "./views/common.js";
import * as game from "./views/game.js";
import * as bracket from "./views/bracket.js";
import * as candidates from "./views/candidates.js";
import * as updown from "./views/updown.js";
import * as generic from "./views/generic.js";

export const LAYOUTS = {
  game_3way: game,
  game_2way: game,
  bracket_ladder: bracket,
  multi_candidates: candidates,
  updown_window: updown,
  binary: generic,
  generic_list: generic,
};
export const layoutFor = (view) => LAYOUTS[view && view.layout] || generic; // unknown shape never fails
const CHART_FIRST = new Set(["bracket_ladder", "multi_candidates", "binary", "generic_list"]);

function indicatorHtml(ls) {
  const age = ls.ageS == null ? "" : " · " + (ls.ageS < 2 ? "now" : ls.ageS + "s ago");
  const src = ls.source ? " · " + (ls.source === "ws" ? "stream" : esc(ls.source)) : "";
  return '<span class="lv-ind ' + ls.state.toLowerCase() + '"><i></i>' + ls.state + '</span><small class="lv-age">' + esc(age.replace(/^ · /, "")) + src + "</small>";
}

/** Mount a live view into `root`. Returns {destroy, state}. */
export function mountLiveView(root, opts) {
  const { client, venue, id } = opts;
  const now = opts.now || (() => Date.now());
  const ui = { tab: "", destroyed: false };
  let sub = null;
  let chartT = null;
  let range = opts.range || "1D";
  root.classList.add("lv");
  root.innerHTML =
    '<div class="lv-head"><div class="lv-title" data-title>Loading…</div><div class="lv-state" data-ind></div></div>' +
    '<div class="lv-note" data-note></div><div class="lv-errbox" data-err></div><div class="lv-chart" data-chart></div><div class="lv-body" data-body></div><div class="lv-chart lv-after" data-chart2></div>';
  const $ = (s) => root.querySelector(s);

  const view = () => (sub && sub.store.view) || null;

  function paintState() {
    const s = sub;
    if (!s) return;
    const ls = liveState({ conn: client.conn, store: s.store, now: now(), restOkAt: s.restOkAt });
    $("[data-ind]").innerHTML = indicatorHtml(ls);
    $("[data-note]").textContent = ls.note;
    const err = $("[data-err]");
    if (!s.store.view && s.error) err.textContent = s.error.detail || s.error.code || "Could not load this event.";
    else if (client.conn === "auth") err.textContent = "Signed out. Sign in again to resume live updates.";
    else err.textContent = "";
  }

  function paintChart() {
    const v = view();
    if (!v) return;
    const first = CHART_FIRST.has(v.layout);
    renderChart(first ? $("[data-chart]") : $("[data-chart2]"), v);
    (first ? $("[data-chart2]") : $("[data-chart]")).innerHTML = "";
  }
  function paintChartSoon() {
    if (chartT) return;
    chartT = setTimeout(() => {
      chartT = null;
      if (!ui.destroyed) paintChart();
    }, 1000);
  }

  function paintAll() {
    const v = view();
    if (!v) return paintState();
    $("[data-title]").textContent = v.title || "";
    $("[data-body]").innerHTML = layoutFor(v).render(v, ui);
    paintChart();
    paintState();
  }

  const onEvent = (evt) => {
    if (ui.destroyed) return;
    const v = view();
    if (evt.type === "snapshot" || evt.type === "rest") paintAll();
    else if (evt.type === "patch" && v) {
      layoutFor(v).update(root, v, evt.changes, ui);
      if (evt.changes.chart || evt.changes.rows.length) paintChartSoon();
      paintState();
    } else paintState();
    if (evt.type === "error" && !v && evt.error && /disabled/i.test(String(evt.error.detail || "")) && opts.onUnavailable) opts.onUnavailable("disabled");
  };
  const offConn = client.onConn((c) => {
    if (c === "disabled" && opts.onUnavailable && !view()) opts.onUnavailable("disabled");
  });
  const unsub = client.subscribe(venue, id, onEvent, range);
  sub = client.subs.get(venue + "|" + id);
  paintState();

  root.addEventListener("click", (e) => {
    const t = e.target.closest("[data-trade],[data-tab],[data-range],[data-open-event]");
    if (!t || !root.contains(t)) return;
    const v = view();
    if (t.dataset.trade && opts.onTrade) opts.onTrade(v ? v.venue : venue, t.dataset.trade, t.dataset.out || "");
    else if (t.dataset.tab && v) {
      ui.tab = t.dataset.tab;
      $("[data-body]").innerHTML = layoutFor(v).render(v, ui);
    } else if (t.dataset.range) {
      range = t.dataset.range;
      client.setRange(venue, id, range);
      for (const b of root.querySelectorAll("[data-range]")) b.classList.toggle("on", b.dataset.range === range);
    } else if (t.dataset.openEvent && opts.onOpenEvent) opts.onOpenEvent(v ? v.venue : venue, t.dataset.openEvent);
  });
  // a crest that fails to load falls back to its initials badge
  root.addEventListener(
    "error",
    (e) => {
      const img = e.target;
      if (img && img.tagName === "IMG" && img.parentElement && img.parentElement.classList.contains("lv-badge")) img.parentElement.textContent = img.parentElement.dataset.ini || "?";
    },
    true
  );

  // pure UI clock: indicator age and the Up/Down countdown. It fetches nothing.
  const clock = setInterval(() => {
    if (ui.destroyed) return;
    paintState();
    const v = view();
    if (v && layoutFor(v).tick) layoutFor(v).tick(root);
  }, 1000);

  return {
    destroy() {
      ui.destroyed = true;
      clearInterval(clock);
      clearTimeout(chartT);
      offConn();
      unsub();
      root.classList.remove("lv");
      root.innerHTML = "";
    },
    state: () => (sub ? liveState({ conn: client.conn, store: sub.store, now: now(), restOkAt: sub.restOkAt }) : null),
  };
}
