const CACHE = "mm-agent-v7";
const SHELL = ["/", "/static/index.html", "/static/manifest.webmanifest", "/static/icon.svg", "/static/css/live.css", "/static/js/main.js", "/static/js/live.js", "/static/js/view.js", "/static/js/chart.js", "/static/js/views/common.js", "/static/js/views/game.js", "/static/js/views/bracket.js", "/static/js/views/candidates.js", "/static/js/views/updown.js", "/static/js/views/generic.js"];
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/v1/")) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy)).catch(() => {});
        return response;
      })
      .catch(() => caches.match(event.request).then((hit) => hit || caches.match("/static/index.html")))
  );
});
