import test from "node:test";
import assert from "node:assert/strict";
import { newStore, applySnapshot, applyPatch, applyStatus, liveState, LiveClient } from "../../src/mm_agent/static/js/live.js";
import { loadView, FakeWS, fakeTimers, snapshotFrame } from "./helpers.mjs";

const store = (name = "bracket") => {
  const s = newStore();
  applySnapshot(s, snapshotFrame(loadView(name), 5), 1000);
  return s;
};
const patch = (view, seq, p) => ({ type: "patch", topic: "event", venue: view.venue, id: view.event_id, seq, source: "ws", server_ts: "x", patch: p });

test("snapshot indexes rows; patch merges nested yes/no and chance", () => {
  const s = store();
  const mid = s.view.sections[0].rows[0].market_id;
  const r = applyPatch(s, patch(s.view, 6, { rows: { [mid]: { chance: "0.9", yes: { ask: "0.91" } } } }), 2000);
  assert.equal(r.action, "render");
  assert.deepEqual(r.changes.rows, [mid]);
  const row = s.rows.get(mid);
  assert.equal(row.chance, "0.9");
  assert.equal(row.yes.ask, "0.91");
  assert.ok(row.yes.bid, "sibling keys survive the merge");
  assert.equal(s.seq, 6);
});

test("a gap in seq asks for a snapshot; old seq is ignored; no view yet asks too", () => {
  const s = store();
  assert.equal(applyPatch(s, patch(s.view, 8, {}), 1).action, "resnapshot");
  assert.equal(s.seq, 5, "a gapped patch is not applied");
  assert.equal(applyPatch(s, patch(s.view, 5, {}), 1).action, "ignore");
  assert.equal(applyPatch(newStore(), patch(s.view, 1, {}), 1).action, "resnapshot");
});

test("game, status, spot, chart tail and freshness patches", () => {
  const s = store("soccer");
  applyPatch(s, patch(s.view, 6, { game: { home: { score: 3 }, period: "HT" }, status: "ended", freshness: { stale: true, source: "rest", price_ts: "t" } }), 1);
  assert.equal(s.view.game.home.score, 3);
  assert.equal(s.view.game.home.name, "Arsenal", "nested merge keeps the rest of the team");
  assert.equal(s.view.game.period, "HT");
  assert.equal(s.view.status, "ended");
  assert.equal(s.view.freshness.stale, true);
  const u = store("updown");
  applyPatch(u, patch(u.view, 6, { spot: { symbol: "BTC", price: "100.5", source: "coinbase" } }), 1);
  assert.equal(u.view.spot.price, "100.5");
  const b = store();
  const rid = b.view.chart.series[0].row_id;
  applyPatch(b, patch(b.view, 6, { chart: { [rid]: [[1793000000, "0.8"]] } }), 1);
  assert.deepEqual(b.view.chart.series[0].points.at(-1), [1793000000, "0.8"]);
});

test("a REST snapshot keeps the chart points already held", () => {
  const s = store();
  s.view.chart.series[0].points = [[1, "0.5"], [2, "0.6"]];
  s.view.chart.history = "ok";
  applySnapshot(s, { view: loadView("bracket"), source: "rest" }, 5, true);
  assert.equal(s.view.chart.series[0].points.length, 2);
  assert.equal(s.source, "rest");
});

test("liveState: LIVE / DELAYED / OFFLINE", () => {
  const s = store();
  s.receivedAt = 10000;
  assert.equal(liveState({ conn: "open", store: s, now: 12000 }).state, "LIVE");
  assert.equal(liveState({ conn: "open", store: s, now: 12000 }).ageS, 2);
  applyStatus(s, { stale: true, source: "rest" }, 10000);
  assert.equal(liveState({ conn: "open", store: s, now: 12000 }).state, "DELAYED");
  assert.equal(liveState({ conn: "closed", store: s, now: 12000, restOkAt: 11000 }).state, "DELAYED");
  assert.equal(liveState({ conn: "closed", store: s, now: 99000, restOkAt: 11000 }).state, "OFFLINE");
  assert.equal(liveState({ conn: "closed", store: newStore(), now: 1 }).state, "OFFLINE");
});

test("sports always carry the vendor 'may be delayed' note", () => {
  const s = store("soccer");
  const ls = liveState({ conn: "open", store: s, now: s.receivedAt + 1000 });
  assert.match(ls.note, /delayed/i);
  assert.equal(ls.state, "LIVE", "the stream is live; the note is about the vendor score data");
  assert.equal(liveState({ conn: "open", store: store("bracket"), now: 1 }).note, "", "no note for non-sports");
});

function mkClient(extra = {}) {
  FakeWS.all.length = 0;
  const tm = fakeTimers();
  const calls = [];
  const client = new LiveClient({
    WebSocket: FakeWS,
    url: "wss://x/v1/stream",
    now: tm.now,
    timers: tm,
    random: () => 1,
    restMs: 5000,
    fetch: async (u) => {
      calls.push(u);
      return extra.fetch ? extra.fetch(u) : { ok: true, json: async () => ({ ok: true, view: loadView("bracket") }) };
    },
  });
  return { client, tm, calls };
}

test("client: subscribe -> snapshot -> patch; indicator flips when the stream is cut and recovers", async () => {
  const { client, tm, calls } = mkClient();
  const v = loadView("bracket");
  const seen = [];
  client.subscribe("polymarket", v.event_id, (e) => seen.push(e.type), "6H");
  const ws = FakeWS.last;
  ws.open();
  assert.deepEqual(ws.sent[0], { op: "subscribe", topic: "event", venue: "polymarket", id: v.event_id, range: "6H" });
  ws.push(snapshotFrame(v, 1));
  const sub = client.subs.get("polymarket|" + v.event_id);
  assert.equal(liveState({ conn: client.conn, store: sub.store, now: tm.now(), restOkAt: sub.restOkAt }).state, "LIVE");
  const mid = v.sections[0].rows[0].market_id;
  ws.push(patch(v, 2, { rows: { [mid]: { chance: "0.1" } } }));
  assert.equal(sub.store.rows.get(mid).chance, "0.1");
  assert.ok(seen.includes("snapshot") && seen.indexOf("patch") > seen.indexOf("snapshot"));
  assert.equal(calls.length, 0, "no REST while the stream delivers");
  // cut the stream
  ws.drop(1006);
  assert.equal(client.conn, "closed");
  await tm.run(1);
  assert.ok(calls.length >= 1, "REST fallback starts");
  assert.equal(liveState({ conn: client.conn, store: sub.store, now: tm.now(), restOkAt: sub.restOkAt }).state, "DELAYED");
  await tm.run(30000);
  assert.ok(FakeWS.all.length >= 2, "reconnects with backoff");
  assert.equal(liveState({ conn: client.conn, store: sub.store, now: tm.now(), restOkAt: sub.restOkAt }).state, "DELAYED");
});

test("client: offline with no REST answer -> OFFLINE; recovery resubscribes and stops REST", async () => {
  const { client, tm, calls } = mkClient({ fetch: () => { throw new Error("down"); } });
  const v = loadView("bracket");
  client.subscribe("polymarket", v.event_id, () => {}, "1D");
  FakeWS.last.drop(1006);
  await tm.run(20000);
  const sub = client.subs.get("polymarket|" + v.event_id);
  assert.equal(liveState({ conn: client.conn, store: sub.store, now: tm.now(), restOkAt: sub.restOkAt }).state, "OFFLINE");
  const ws = FakeWS.last;
  ws.open();
  assert.equal(ws.sent[0].op, "subscribe");
  const n = calls.length;
  ws.push(snapshotFrame(v, 3));
  await tm.run(20000);
  assert.equal(calls.length, n, "REST stops once the stream delivers again");
  assert.equal(liveState({ conn: client.conn, store: sub.store, now: tm.now(), restOkAt: sub.restOkAt }).state, "LIVE");
});

test("client: a seq gap sends a snapshot request; the range change does too", () => {
  const { client } = mkClient();
  const v = loadView("bracket");
  client.subscribe("polymarket", v.event_id, () => {}, "1D");
  const ws = FakeWS.last;
  ws.open();
  ws.push(snapshotFrame(v, 1));
  ws.push(patch(v, 5, {}));
  assert.equal(ws.sent.at(-1).op, "snapshot");
  client.setRange("polymarket", v.event_id, "1W");
  assert.deepEqual(ws.sent.at(-1), { op: "snapshot", topic: "event", venue: "polymarket", id: v.event_id, range: "1W" });
});

test("client: patches follow the canonical id when the view was opened by slug", () => {
  const { client } = mkClient();
  const v = loadView("bracket");
  client.subscribe("polymarket", v.slug, () => {}, "1D");
  const ws = FakeWS.last;
  ws.open();
  ws.push({ ...snapshotFrame(v, 1), id: v.event_id, requested: v.slug });
  const mid = v.sections[0].rows[0].market_id;
  ws.push(patch(v, 2, { rows: { [mid]: { chance: "0.33" } } }));
  assert.equal(client.subs.get("polymarket|" + v.slug).store.rows.get(mid).chance, "0.33");
});

test("client: 4404 = live views disabled, 4401 = signed out; neither reconnects", async () => {
  for (const [code, state] of [[4404, "disabled"], [4401, "auth"]]) {
    const { client, tm } = mkClient();
    client.subscribe("polymarket", "1", () => {}, "1D");
    const n = FakeWS.all.length;
    FakeWS.last.drop(code);
    await tm.run(60000);
    assert.equal(client.conn, state);
    assert.equal(FakeWS.all.length, n);
  }
});

test("client: last unsubscribe closes the socket", () => {
  const { client } = mkClient();
  const off = client.subscribe("polymarket", "1", () => {}, "1D");
  const ws = FakeWS.last;
  ws.open();
  off();
  assert.equal(ws.readyState, 3);
  assert.equal(client.subs.size, 0);
});
