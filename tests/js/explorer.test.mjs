import test from "node:test";
import assert from "node:assert/strict";
import {
  buildQuery,
  missing,
  exploreUrl,
  accountUrl,
  rowsOf,
  nextPage,
  seriesPoints,
  renderResult,
  tableHtml,
  mountMarketInsights,
  writeStatusHtml,
} from "../../src/mm_agent/static/js/explorer.js";

const route = (o = {}) => ({ key: "data.trades", params: [{ name: "user" }, { name: "limit" }], path_params: [], needs_any: [], needs_one: [], paging: "cursor", host: "data", result: "table", ...o });

test("buildQuery drops empty values and trims", () => {
  const q = buildQuery(route(), { user: " 0xabc ", limit: "", other: "x" });
  assert.equal(q.toString(), "user=0xabc");
});

test("missing reports path params, needs_any and needs_one", () => {
  assert.deepEqual(missing(route({ path_params: ["id"] }), {}), ["id"]);
  assert.deepEqual(missing(route({ needs_any: ["user", "condition"] }), {}), ["user or condition"]);
  assert.deepEqual(missing(route({ needs_any: ["user"] }), { user: "0x1" }), []);
  assert.equal(missing(route({ needs_one: ["a", "b"] }), { a: "1", b: "2" }).length, 1);
});

test("urls: explore encodes the key, account strips its prefix", () => {
  assert.equal(exploreUrl(route(), { user: "0x1" }), "/v1/polymarket/explore/data.trades?user=0x1");
  assert.equal(accountUrl({ key: "account.rewards.day", params: [{ name: "date" }] }, { date: "2026-11-01" }), "/v1/polymarket/account/rewards.day?date=2026-11-01");
});

test("rowsOf finds the list in every answer shape", () => {
  assert.deepEqual(rowsOf([1]), [1]);
  assert.deepEqual(rowsOf({ data: [1], pagination: {} }), [1]);
  assert.deepEqual(rowsOf({ markets: [2], next_cursor: "c" }), [2]);
  assert.deepEqual(rowsOf({ only: [3], n: 1 }), [3]);
  assert.equal(rowsOf({ a: 1 }), null);
  assert.equal(rowsOf(null), null);
});

test("nextPage: data cursor, keyset, clob cursor, offset, and the end marker", () => {
  assert.deepEqual(nextPage(route(), { data: [], pagination: { has_more: true, next_cursor: "n1" } }, { user: "u" }), { user: "u", cursor: "n1" });
  assert.equal(nextPage(route(), { data: [], pagination: { has_more: false, next_cursor: null } }, {}), null);
  assert.deepEqual(nextPage(route({ paging: "keyset", host: "gamma" }), { markets: [], next_cursor: "k" }, {}), { after_cursor: "k" });
  assert.deepEqual(nextPage(route({ paging: "cursor", host: "clob" }), { data: [], next_cursor: "c" }, {}), { next_cursor: "c" });
  assert.equal(nextPage(route({ paging: "cursor", host: "clob" }), { data: [], next_cursor: "LTE=" }, {}), null);
  assert.deepEqual(nextPage(route({ paging: "offset", host: "gamma" }), [1, 2], { limit: "2" }), { limit: "2", offset: "2" });
  assert.equal(nextPage(route({ paging: "offset", host: "gamma" }), [1], { limit: "2" }), null);
  assert.deepEqual(nextPage(route({ paging: "offset", host: "gamma" }), { items: [1, 2] }, { limit: "2" }), { limit: "2", offset: "2" });
});

test("seriesPoints reads pairs and objects and drops junk", () => {
  assert.deepEqual(seriesPoints({ history: [[1, 0.5], [2, "0.6"]] }), [[1, 0.5], [2, 0.6]]);
  assert.deepEqual(seriesPoints({ data: [{ t: 10, p: 0.2 }, { t: "x", p: 1 }, { foo: 1 }] }), [[10, 0.2]]);
  assert.deepEqual(seriesPoints("nope"), []);
});

test("renderResult picks text, series, table or object and escapes markup", () => {
  assert.match(renderResult({ result: "text" }, { ok: true, result: "1767225600" }), /1767225600/);
  assert.match(renderResult({ result: "series" }, { ok: true, result: { data: [{ t: 1, p: 1 }, { t: 2, p: 2 }] } }), /<svg/);
  assert.match(renderResult({ result: "table" }, { ok: true, result: { data: [{ a: 1 }] } }), /<table/);
  const html = renderResult({ result: "object" }, { ok: true, result: { name: "<img src=x onerror=alert(1)>" } });
  assert.ok(!html.includes("<img"));
  assert.match(html, /&lt;img/);
});

test("tableHtml handles empty and non-object rows", () => {
  assert.match(tableHtml([]), /No rows/);
  assert.match(tableHtml([1, 2]), /<pre/);
});

test("market insights: non-polymarket or bad id shows a note and makes no request", async () => {
  const el = { innerHTML: "" };
  let calls = 0;
  await mountMarketInsights(el, { venue: "kalshi", native_id: "X" }, { fetch: async () => (calls++, { ok: true, json: async () => ({}) }) });
  assert.match(el.innerHTML, /Polymarket markets/);
  assert.equal(calls, 0);
});

test("market insights: asks holders, oi, resolutions, info, fee and tick", async () => {
  const el = { innerHTML: "" };
  const seen = [];
  const cond = "0x" + "ab".repeat(32);
  await mountMarketInsights(el, { venue: "polymarket", native_id: cond, outcomes: [{ native_id: "777" }] }, {
    fetch: async (u) => (seen.push(u), { ok: true, json: async () => ({ result: { data: [{ x: 1 }] } }) }),
  });
  assert.equal(seen.length, 6);
  assert.ok(seen.some((u) => u.includes("data.holders") && u.includes("include_pnl=true")));
  assert.ok(seen.some((u) => u.includes("clob.fee_rate") && u.includes("token_id=777")));
  assert.match(el.innerHTML, /Top holders/);
});

test("writeStatusHtml lists every action as a status line, never a button", () => {
  const html = writeStatusHtml({
    enabled: false,
    heartbeat: { running: false },
    actions: [
      { key: "orders.cancel_ids", enabled: false, ack: "" },
      { key: "keys.api.delete", enabled: true, ack: "DELETE API KEY" },
    ],
  });
  assert.match(html, /Write API off/);
  assert.match(html, /orders\.cancel_ids/);
  assert.match(html, /enabled · typed text/);
  assert.doesNotMatch(html, /<button|<form|<input/);
  assert.equal(writeStatusHtml(null), "");
  assert.equal(writeStatusHtml({}), "");
});
