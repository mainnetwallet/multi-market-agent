/* Multi-line probability chart (SVG). Points are [t, "p"]: t in seconds (or ms), p a probability string. Midpoint series. */
import { esc, num } from "./views/common.js";

export const COLORS = ["#3b7bff", "#ff6a2b", "#27ae60", "#f5c451", "#a66cff", "#12b5cb"];

export function toMs(t) {
  const n = Number(t);
  return n < 1e12 ? n * 1000 : n;
}

/** Pick the lines to draw: top `n` by current chance (chart legend = top 4). Pure. */
export function pickSeries(view, n = 4) {
  const chart = view.chart;
  if (!chart) return [];
  const chance = new Map();
  for (const s of view.sections || []) for (const r of s.rows || []) chance.set(String(r.market_id), num(r.chance));
  const lines = (chart.series || []).map((s, i) => ({
    id: String(s.row_id),
    name: s.label,
    color: s.color || COLORS[i % COLORS.length],
    chance: chance.get(String(s.row_id)),
    pts: (s.points || []).map((p) => ({ t: toMs(p[0]), v: num(p[1]) * 100 })).filter((p) => Number.isFinite(p.v) && Number.isFinite(p.t)),
  }));
  lines.sort((a, b) => (Number.isNaN(b.chance) ? -1 : b.chance) - (Number.isNaN(a.chance) ? -1 : a.chance));
  return lines.slice(0, n).map((l, i) => ({ ...l, color: chart.series.length > n ? COLORS[i % COLORS.length] : l.color }));
}

export function renderChart(box, view, opts = {}) {
  if (!box) return;
  const chart = view.chart;
  if (!chart) {
    box.innerHTML = "";
    return;
  }
  const ranges = chart.ranges || ["1H", "6H", "1D", "1W", "1M", "ALL"];
  const cur = chart.range || chart.default || "1D";
  const bar = '<div class="lv-ranges">' + ranges.map((r) => '<button data-range="' + esc(r) + '" class="' + (r === cur ? "on" : "") + '">' + esc(r) + "</button>").join("") + "</div>";
  if (chart.history === "unavailable") {
    box.innerHTML = '<div class="lv-chartmsg">History not available for this venue.</div>' + bar;
    return;
  }
  const S = pickSeries(view, opts.top || 4).filter((s) => s.pts.length > 1);
  if (!S.length) {
    box.innerHTML = '<div class="lv-chartmsg">Not enough history for this range.</div>' + bar;
    return;
  }
  const W = Math.max(280, Math.round(box.clientWidth || 640)),
    H = opts.h || 220,
    L = 6,
    R = 40,
    T = 10,
    B = 18;
  const all = S.flatMap((s) => s.pts);
  const t0 = Math.min(...all.map((p) => p.t)),
    t1 = Math.max(...all.map((p) => p.t));
  const vmax = Math.max(...all.map((p) => p.v)),
    vmin = Math.min(...all.map((p) => p.v));
  const hi = Math.min(100, Math.ceil((vmax + 4) / 5) * 5),
    lo = Math.max(0, Math.floor((vmin - 4) / 5) * 5);
  const X = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const Y = (v) => T + ((hi - v) / Math.max(1e-9, hi - lo)) * (H - T - B);
  let g = "";
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v).toFixed(1) + '" y2="' + Y(v).toFixed(1) + '" class="lv-gl"/><text x="' + (W - R + 5) + '" y="' + (Y(v) + 4).toFixed(1) + '" class="lv-gt">' + Math.round(v) + "%</text>";
  }
  for (const s of S) {
    const d = s.pts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p.v).toFixed(1)).join(" ");
    const e = s.pts[s.pts.length - 1];
    g += '<path d="' + d + '" class="lv-ln" style="stroke:' + esc(s.color) + '"/><circle cx="' + X(e.t).toFixed(1) + '" cy="' + Y(e.v).toFixed(1) + '" r="3.5" style="fill:' + esc(s.color) + '"/>';
  }
  const legend = '<div class="lv-legend">' + S.map((s) => '<span style="color:' + esc(s.color) + '"><i style="background:' + esc(s.color) + '"></i>' + esc(s.name) + " <b>" + s.pts[s.pts.length - 1].v.toFixed(1) + "%</b></span>").join("") + "</div>";
  box.innerHTML = legend + '<svg viewBox="0 0 ' + W + " " + H + '" class="lv-plot" role="img" aria-label="Probability chart">' + g + '<line class="lv-cx" y1="' + T + '" y2="' + (H - B) + '" x1="0" x2="0" style="display:none"/></svg><div class="lv-tip lv-hidden"></div>' + bar;
  const svg = box.querySelector("svg"),
    vx = box.querySelector(".lv-cx"),
    tip = box.querySelector(".lv-tip");
  const near = (s, t) => s.pts.reduce((b, p) => (Math.abs(p.t - t) < Math.abs(b.t - t) ? p : b), s.pts[0]);
  svg.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect(),
      x = ((ev.clientX - r.left) / r.width) * W,
      t = t0 + ((x - L) / Math.max(1, W - L - R)) * (t1 - t0),
      cx = Math.min(Math.max(X(t), L), W - R);
    vx.setAttribute("x1", cx);
    vx.setAttribute("x2", cx);
    vx.style.display = "";
    tip.classList.remove("lv-hidden");
    tip.textContent = new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Dhaka", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) + " · " + S.map((s) => s.name + " " + near(s, t).v.toFixed(1) + "%").join(" · ");
  });
  svg.addEventListener("pointerleave", () => {
    vx.style.display = "none";
    tip.classList.add("lv-hidden");
  });
}
