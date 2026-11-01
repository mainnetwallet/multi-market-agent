import test from "node:test";
import assert from "node:assert/strict";
import { LiveClient } from "../../src/mm_agent/static/js/live.js";
import { LAYOUTS, layoutFor } from "../../src/mm_agent/static/js/view.js";
import { pickSeries, toMs } from "../../src/mm_agent/static/js/chart.js";
import { initials, badge, pct, cents, change, esc } from "../../src/mm_agent/static/js/views/common.js";
import { countdown } from "../../src/mm_agent/static/js/views/updown.js";
import { loadView, FakeWS, snapshotFrame } from "./helpers.mjs";

/* every layout renders from an EventView that came over a fake stream */
const CASES = [
  ["soccer", "game_3way"],
  ["nba", "game_2way"],
  ["bracket", "bracket_ladder"],
  ["candidates", "multi_candidates"],
  ["updown", "updown_window"],
  ["binary", "binary"],
];

for (const [name, layout] of CASES) {
  test("layout " + layout + " renders from the stream snapshot", () => {
    FakeWS.all.length = 0;
    const client = new LiveClient({ WebSocket: FakeWS, url: "wss://x/v1/stream", fetch: async () => ({ ok: false, json: async () => ({}) }) });
    const v = loadView(name);
    const off = client.subscribe(v.venue, v.event_id, () => {}, "1D");
    FakeWS.last.open();
    FakeWS.last.push(snapshotFrame(v, 1));
    const view = client.subs.get(v.venue + "|" + v.event_id).store.view;
    assert.equal(view.layout, layout);
    const html = layoutFor(view).render(view, { tab: "" });
    assert.ok(html.length > 50);
    for (const r of view.sections.flatMap((s) => s.rows)) {
      assert.ok(html.includes('data-trade="' + r.market_id + '"') || layout === "game_3way" || layout === "game_2way", "every row can open the trade sheet: " + r.market_id);
    }
    assert.ok(!html.includes("undefined") && !html.includes("NaN"), "no undefined/NaN in " + layout);
    off(); // closes the socket and its timers
  });
}

test("soccer shows Home / Draw / Away heroes, the score and a tab per non-empty section", () => {
  const v = loadView("soccer");
  const html = LAYOUTS.game_3way.render(v, { tab: "" });
  assert.equal((html.match(/class="lv-hero /g) || []).length, 3);
  assert.ok(html.includes('data-g="home">2<'));
  assert.ok(html.includes('data-g="away">1<'));
  assert.equal((html.match(/data-tab="/g) || []).length, v.sections.filter((s) => s.rows.length).length);
  assert.ok(LAYOUTS.game_3way.render(v, { tab: "halves" }).includes("lv-tab on"));
});

test("unknown layout falls back to the generic list (never fails)", () => {
  const v = { ...loadView("binary"), layout: "something_new" };
  assert.equal(layoutFor(v), LAYOUTS.generic_list);
  assert.ok(layoutFor(v).render(v, { tab: "" }).includes("lv-row"));
});

test("bracket: ascending order, Buy Yes / Buy No at the ask", () => {
  const v = loadView("bracket");
  const html = LAYOUTS.bracket_ladder.render(v);
  const labels = [...html.matchAll(/<b>(\$[^<]+)<\/b>/g)].map((m) => m[1]);
  assert.deepEqual(labels, v.sections[0].rows.map((r) => r.label));
  const r = v.sections[0].rows[0];
  assert.ok(html.includes(">" + cents(r.yes.ask) + "<"));
});

test("chart: top 4 lines by chance, seconds -> ms, bad points dropped", () => {
  const v = loadView("bracket");
  v.chart.series.forEach((s, i) => (s.points = [[1793000000 + i, "0.5"], [1793000100, "x"], [1793000200, "0.6"]]));
  const lines = pickSeries(v, 4);
  assert.ok(lines.length <= 4);
  assert.equal(lines[0].pts[0].t, 1793000000 * 1000 + (lines[0].id === v.chart.series[0].row_id ? 0 : 1000 * v.chart.series.findIndex((s) => s.row_id === lines[0].id)));
  assert.equal(lines[0].pts.length, 2);
  assert.equal(toMs(1793000000), 1793000000000);
  assert.equal(toMs(1793000000000), 1793000000000);
});

test("formatting helpers", () => {
  assert.equal(pct("0.555"), "56%");
  assert.equal(pct("0.004"), "<1%");
  assert.equal(pct(null), "—");
  assert.equal(cents("0.56"), "56¢");
  assert.deepEqual(change("0.02"), { text: "+2.0 pp", cls: "pos" });
  assert.equal(change("0"), null);
  assert.equal(initials("Real Madrid"), "RM");
  assert.equal(initials(""), "?");
  assert.ok(badge("Arsenal", "https://x/a.png").includes("<img"));
  assert.ok(!badge("Arsenal", "javascript:alert(1)").includes("<img"), "only https logos");
  assert.ok(!badge("Arsenal", null).includes("<img"));
  assert.equal(esc('<img onerror="x">'), "&lt;img onerror=&quot;x&quot;&gt;");
  assert.equal(countdown(100, 40000), "0:60".replace("0:60", "1:00"));
});

test("hostile labels are escaped in every layout", () => {
  const v = loadView("candidates");
  v.sections[0].rows[0].label = '<script>alert(1)</script>';
  v.title = "<b>x</b>";
  const html = LAYOUTS.multi_candidates.render(v);
  assert.ok(!html.includes("<script>"));
});
