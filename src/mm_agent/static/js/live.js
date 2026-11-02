/* Live event store: snapshot/patch reducer + one shared /v1/stream client with REST fallback.
   Display data only. Trading always goes through the existing trade sheet and /v1/orders/*. */

export const RANGES = ["1H", "6H", "1D", "1W", "1M", "ALL"];
export const REST_MIN_MS = 5000; // fallback poll: never faster than 2 s per event, we use 5 s
const IDLE_PING_MS = 45000; // quiet socket: ask for a snapshot as a liveness probe
const PING_REPLY_MS = 10000;

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const clone = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

/* ------------------------------------------------------------------ store + reducer */

export function newStore() {
  return { view: null, seq: -1, source: "", sourceTs: null, serverTs: null, rows: new Map(), receivedAt: 0 };
}

function indexRows(store) {
  store.rows = new Map();
  for (const sec of (store.view && store.view.sections) || []) for (const r of sec.rows || []) store.rows.set(String(r.market_id), r);
}

/** Replace the whole view. `keepChart`: REST snapshots carry no history, so keep the points we already hold. */
export function applySnapshot(store, frame, now = Date.now(), keepChart = false) {
  const view = clone(frame.view);
  if (keepChart && store.view && store.view.chart && view.chart) {
    const old = new Map((store.view.chart.series || []).map((s) => [s.row_id, s.points]));
    for (const s of view.chart.series || []) if (!(s.points || []).length && old.has(s.row_id)) s.points = old.get(s.row_id);
    view.chart.history = store.view.chart.history;
    view.chart.range = store.view.chart.range;
  }
  store.view = view;
  store.seq = typeof frame.seq === "number" ? frame.seq : store.seq;
  store.source = frame.source || (view.freshness && view.freshness.source) || "";
  store.sourceTs = frame.source_ts || null;
  store.serverTs = frame.server_ts || null;
  store.receivedAt = now;
  indexRows(store);
  return { kind: "snapshot", rows: [...store.rows.keys()], game: !!view.game, chart: true, status: true, spot: true };
}

function mergeDeep(target, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (isObj(v) && isObj(target[k])) mergeDeep(target[k], v);
    else target[k] = clone(v);
  }
}

/** Apply a patch frame. Returns {action:"render"|"ignore"|"resnapshot", changes}. A gap in seq asks for a snapshot. */
export function applyPatch(store, frame, now = Date.now()) {
  if (!store.view) return { action: "resnapshot", changes: null };
  if (typeof frame.seq === "number") {
    if (frame.seq <= store.seq) return { action: "ignore", changes: null };
    if (frame.seq > store.seq + 1) return { action: "resnapshot", changes: null };
  }
  const p = frame.patch || {};
  const changes = { kind: "patch", rows: [], game: false, chart: false, status: false, spot: false };
  for (const [mid, rp] of Object.entries(p.rows || {})) {
    const row = store.rows.get(String(mid));
    if (!row) continue;
    mergeDeep(row, rp);
    changes.rows.push(String(mid));
  }
  if (isObj(p.game) && store.view.game) {
    mergeDeep(store.view.game, p.game);
    changes.game = true;
  }
  if (typeof p.status === "string") {
    store.view.status = p.status;
    changes.status = true;
  }
  if (isObj(p.spot)) {
    store.view.spot = clone(p.spot);
    changes.spot = true;
  }
  if (isObj(p.freshness)) {
    store.view.freshness = clone(p.freshness);
    changes.status = true;
  }
  if (isObj(p.chart) && store.view.chart) {
    for (const [mid, pts] of Object.entries(p.chart)) {
      const line = (store.view.chart.series || []).find((s) => String(s.row_id) === String(mid));
      if (!line || !Array.isArray(pts)) continue;
      line.points = (line.points || []).concat(clone(pts)).slice(-2000);
      changes.chart = true;
    }
  }
  if (typeof frame.seq === "number") store.seq = frame.seq;
  store.source = frame.source || store.source;
  store.sourceTs = frame.source_ts || store.sourceTs;
  store.serverTs = frame.server_ts || store.serverTs;
  store.receivedAt = now;
  return { action: "render", changes };
}

/** `status` frame: the hub switched between ws and rest, or marked the event stale. */
export function applyStatus(store, frame, now = Date.now()) {
  if (!store.view) return false;
  store.view.freshness = { ...(store.view.freshness || {}), stale: !!frame.stale, source: frame.source || store.source };
  store.source = frame.source || store.source;
  store.receivedAt = now;
  return true;
}

/* ------------------------------------------------------------------ LIVE / DELAYED / OFFLINE */

/** Pure. conn: "open"|"connecting"|"closed"|"disabled"|"auth". restOkAt: epoch ms of the last good REST poll (0 = never). */
export function liveState({ conn, store, now, restOkAt = 0 }) {
  const v = store && store.view;
  if (!v) return { state: "OFFLINE", source: "", ageS: null, note: "" };
  const age = store.receivedAt ? Math.max(0, Math.round((now - store.receivedAt) / 1000)) : null;
  const fresh = v.freshness || {};
  const sports = !!v.game;
  const note = sports ? "Vendor score data may be delayed." : "";
  if (conn === "open") {
    if (fresh.stale || store.source === "rest") return { state: "DELAYED", source: "rest", ageS: age, note };
    return { state: "LIVE", source: store.source || "ws", ageS: age, note };
  }
  if (restOkAt && now - restOkAt < 3 * REST_MIN_MS) return { state: "DELAYED", source: "rest", ageS: age, note };
  return { state: "OFFLINE", source: store.source || "", ageS: age, note };
}

/* ------------------------------------------------------------------ shared client */

export class LiveClient {
  constructor(opts = {}) {
    this.url = opts.url || "/v1/stream";
    this.WS = opts.WebSocket || (typeof WebSocket !== "undefined" ? WebSocket : null);
    this.fetch = opts.fetch || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : null);
    this.now = opts.now || (() => Date.now());
    this.timers = opts.timers || { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (t) => clearTimeout(t) };
    this.random = opts.random || Math.random;
    this.restMs = opts.restMs || REST_MIN_MS;
    this.conn = "closed";
    this.subs = new Map(); // key venue|requested -> sub
    this.canon = new Map(); // venue|canonical id -> sub key
    this.ws = null;
    this.attempt = 0;
    this.retryT = null;
    this.idleT = null;
    this.pingT = null;
    this.wanted = false;
    this.connListeners = new Set();
  }

  onConn(fn) {
    this.connListeners.add(fn);
    return () => this.connListeners.delete(fn);
  }
  _setConn(c) {
    if (this.conn === c) return;
    this.conn = c;
    for (const fn of this.connListeners) fn(c);
    for (const sub of this.subs.values()) this._emit(sub, { type: "conn" });
    this._syncRest();
  }

  /** Follow an event. `fn(evt, sub)` gets {type:"snapshot"|"patch"|"status"|"conn"|"error"|"rest", changes?}. Returns unsubscribe. */
  subscribe(venue, id, fn, range = "1D") {
    const key = venue + "|" + id;
    let sub = this.subs.get(key);
    if (!sub) {
      sub = { key, venue, id, range, store: newStore(), fns: new Set(), restT: null, restOkAt: 0, restBusy: false, error: null };
      this.subs.set(key, sub);
      this._ensureSocket();
      if (this.conn === "open") this._send({ op: "subscribe", topic: "event", venue, id, range });
    }
    sub.fns.add(fn);
    if (sub.store.view) fn({ type: "snapshot", changes: { kind: "snapshot", rows: [...sub.store.rows.keys()], chart: true, status: true, spot: true, game: true } }, sub);
    this._syncRest();
    return () => {
      sub.fns.delete(fn);
      if (!sub.fns.size) this._drop(sub);
    };
  }

  setRange(venue, id, range) {
    const sub = this.subs.get(venue + "|" + id);
    if (!sub) return;
    sub.range = range;
    if (this.conn === "open") this._send({ op: "snapshot", topic: "event", venue, id, range });
  }

  _drop(sub) {
    this.subs.delete(sub.key);
    this._stopRest(sub);
    for (const [k, v] of [...this.canon]) if (v === sub.key) this.canon.delete(k);
    if (this.conn === "open") this._send({ op: "unsubscribe", topic: "event", venue: sub.venue, id: sub.id });
    if (!this.subs.size) this._closeSocket();
  }

  _emit(sub, evt) {
    for (const fn of [...sub.fns]) {
      try {
        fn(evt, sub);
      } catch (e) {
        /* a broken view must not stop the stream */
      }
    }
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }

  _ensureSocket() {
    if (this.ws || !this.WS || this.conn === "disabled" || this.conn === "auth") return;
    this.wanted = true;
    this._setConn("connecting");
    let ws;
    try {
      const base = typeof location !== "undefined" ? location : { protocol: "https:", host: "localhost" };
      const abs = /^wss?:/.test(this.url) ? this.url : (base.protocol === "https:" ? "wss://" : "ws://") + base.host + this.url;
      ws = new this.WS(abs);
    } catch (e) {
      this._setConn("closed");
      this._scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this._setConn("open");
      for (const s of this.subs.values()) this._send({ op: "subscribe", topic: "event", venue: s.venue, id: s.id, range: s.range });
      this._armIdle();
    };
    ws.onmessage = (m) => {
      let f;
      try {
        f = JSON.parse(m.data);
      } catch (e) {
        return;
      }
      this._armIdle();
      this._frame(f);
    };
    ws.onclose = (e) => {
      if (this.ws === ws) this.ws = null;
      this._clearIdle();
      const code = e && e.code;
      if (code === 4401) return this._setConn("auth");
      if (code === 4404) return this._setConn("disabled");
      this._setConn("closed");
      if (this.wanted && this.subs.size) this._scheduleRetry();
    };
    ws.onerror = () => {};
  }

  _closeSocket() {
    this.wanted = false;
    this._clearIdle();
    this.timers.clearTimeout(this.retryT);
    this.retryT = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      try {
        ws.close();
      } catch (e) {}
    }
    this.conn = "closed";
  }

  _scheduleRetry() {
    if (this.retryT) return;
    const base = Math.min(15000, 1000 * 2 ** Math.min(this.attempt, 4));
    this.attempt += 1;
    this.retryT = this.timers.setTimeout(() => {
      this.retryT = null;
      if (this.wanted && this.subs.size) this._ensureSocket();
    }, Math.round(base * (0.5 + this.random() * 0.5)));
  }

  _armIdle() {
    this._clearIdle();
    this.idleT = this.timers.setTimeout(() => {
      // quiet socket: ask a subscribed event for a snapshot; no answer -> treat the socket as dead and reconnect
      const first = this.subs.values().next().value;
      if (!first) return;
      this._send({ op: "snapshot", topic: "event", venue: first.venue, id: first.id, range: first.range });
      this.pingT = this.timers.setTimeout(() => {
        const ws = this.ws;
        if (ws) {
          try {
            ws.close();
          } catch (e) {}
        }
      }, PING_REPLY_MS);
    }, IDLE_PING_MS);
  }
  _clearIdle() {
    this.timers.clearTimeout(this.idleT);
    this.timers.clearTimeout(this.pingT);
    this.idleT = this.pingT = null;
  }

  _subFor(f) {
    const key = this.canon.get(f.venue + "|" + f.id) || f.venue + "|" + f.id;
    return this.subs.get(key) || null;
  }

  _frame(f) {
    if (!f || typeof f !== "object") return;
    const sub = this._subFor(f);
    if (f.type === "snapshot" && f.requested !== undefined) {
      const rk = f.venue + "|" + f.requested;
      const s = this.subs.get(rk);
      if (s) {
        this.canon.set(f.venue + "|" + f.id, rk);
        const changes = applySnapshot(s.store, f, this.now());
        this._stopRest(s);
        s.error = null;
        this._emit(s, { type: "snapshot", changes });
      }
      return;
    }
    if (!sub) return;
    if (f.type === "patch") {
      const r = applyPatch(sub.store, f, this.now());
      if (r.action === "resnapshot") this._send({ op: "snapshot", topic: "event", venue: sub.venue, id: sub.id, range: sub.range });
      else if (r.action === "render") this._emit(sub, { type: "patch", changes: r.changes });
    } else if (f.type === "status") {
      if (applyStatus(sub.store, f, this.now())) this._emit(sub, { type: "status", changes: { kind: "status", rows: [], status: true } });
    } else if (f.type === "error") {
      sub.error = { code: f.code, detail: f.detail };
      this._emit(sub, { type: "error", error: sub.error });
      if (f.code === "not_found") this._startRest(sub);
    }
  }

  /* -------------------------------------------- REST fallback (only while the socket cannot deliver) */
  _needRest(sub) {
    return this.conn !== "open" || !sub.store.view;
  }
  _syncRest() {
    for (const sub of this.subs.values()) {
      if (this._needRest(sub) && this.conn !== "connecting") this._startRest(sub);
      else if (!this._needRest(sub)) this._stopRest(sub);
    }
  }
  _startRest(sub) {
    if (sub.restT || !this.fetch) return;
    const tick = async () => {
      sub.restT = this.timers.setTimeout(tick, this.restMs);
      if (sub.restBusy) return;
      sub.restBusy = true;
      try {
        const r = await this.fetch("/v1/event/view?venue=" + encodeURIComponent(sub.venue) + "&id=" + encodeURIComponent(sub.id), { credentials: "same-origin" });
        if (!r.ok) {
          let detail = "";
          try {
            detail = (await r.json()).detail || "";
          } catch (e) {}
          sub.error = { code: "http_" + r.status, detail: String(detail) };
          this._emit(sub, { type: "error", error: sub.error });
          return;
        }
        const body = await r.json();
        const view = body.view || body;
        const changes = applySnapshot(sub.store, { view, source: "rest", server_ts: new Date(this.now()).toISOString() }, this.now(), true);
        sub.store.source = "rest";
        sub.restOkAt = this.now();
        sub.error = null;
        this._emit(sub, { type: "rest", changes });
      } catch (e) {
        /* offline: the indicator turns OFFLINE once restOkAt is old */
      } finally {
        sub.restBusy = false;
      }
    };
    sub.restT = this.timers.setTimeout(tick, 0);
  }
  _stopRest(sub) {
    if (sub.restT) this.timers.clearTimeout(sub.restT);
    sub.restT = null;
  }
}
