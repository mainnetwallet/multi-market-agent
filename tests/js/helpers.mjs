import fs from "node:fs";
import path from "node:path";

export const FIX = process.env.EVENTVIEW_FIXTURES || path.join(path.dirname(new URL(import.meta.url).pathname), "views");
export const loadView = (name) => JSON.parse(fs.readFileSync(path.join(FIX, name + ".json"), "utf8"));

export class FakeWS {
  static last = null;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    FakeWS.last = this;
    FakeWS.all.push(this);
  }
  send(s) {
    this.sent.push(JSON.parse(s));
  }
  close() {
    this.readyState = 3;
    if (this.onclose) this.onclose({ code: 1000 });
  }
  open() {
    this.readyState = 1;
    this.onopen();
  }
  push(obj) {
    this.onmessage({ data: JSON.stringify(obj) });
  }
  drop(code = 1006) {
    this.readyState = 3;
    if (this.onclose) this.onclose({ code });
  }
}
FakeWS.all = [];

/** Deterministic timers: run(ms) advances the clock and fires what is due. */
export function fakeTimers() {
  let t = 1_000_000, // like a real epoch clock: 0 would read as 'never'
    id = 0;
  const q = new Map();
  return {
    now: () => t,
    setTimeout(fn, ms) {
      id += 1;
      q.set(id, { at: t + (ms || 0), fn });
      return id;
    },
    clearTimeout(h) {
      q.delete(h);
    },
    async run(ms) {
      const end = t + ms;
      for (;;) {
        const due = [...q.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        q.delete(due[0]);
        t = Math.max(t, due[1].at);
        await due[1].fn();
        await new Promise((r) => setImmediate(r)); // let pending fetch promises settle
      }
      t = end;
    },
    pending: () => q.size,
  };
}

export function snapshotFrame(view, seq = 1, extra = {}) {
  return { type: "snapshot", topic: "event", venue: view.venue, id: view.event_id, requested: view.event_id, seq, source: "ws", source_ts: null, server_ts: "2026-11-01T12:00:00Z", range: "1D", view, ...extra };
}
