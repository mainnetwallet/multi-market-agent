/* Entry point: exposes window.MMLive to the classic script in index.html (trade sheet, discover cards, search box). */
import { LiveClient } from "./live.js";
import { mountLiveView } from "./view.js";

const client = new LiveClient();
const URL_RE = /^https:\/\/(www\.)?(polymarket\.com|kalshi\.com|limitless\.exchange)\//i;

async function viewFromUrl(url) {
  const r = await fetch("/v1/event/view?url=" + encodeURIComponent(url), { credentials: "same-origin" });
  let body = {};
  try {
    body = await r.json();
  } catch (e) {}
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "HTTP " + r.status);
  return body.view;
}

window.MMLive = {
  client,
  mount: (root, opts) => mountLiveView(root, { client, ...opts }),
  isEventUrl: (s) => URL_RE.test(String(s || "").trim()),
  resolveUrl: async (url) => {
    const v = await viewFromUrl(url.trim());
    return { venue: v.venue, id: String(v.event_id || v.slug) };
  },
};
document.dispatchEvent(new Event("mmlive-ready"));
