// v2 (2026-09-25): the page shell ("/") used to be cache-first, so once a
// phone had it cached, every later deploy (drill-down, reviews, monthly
// special) silently never showed up -- this file's own bytes hadn't
// changed, so the browser never even re-checked for an update. Fix: "/" is
// now network-first too, same pattern as /api/data, so a new deploy shows
// up on next load and the app still works offline from the last-seen copy.
const CACHE = "tt-pulse-v2";
const SHELL = ["/manifest.json", "/static/icon-192.png", "/static/icon-512.png"];
const NETWORK_FIRST = new Set(["/", "/api/data"]);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (NETWORK_FIRST.has(url.pathname)) {
    // network-first, fall back to the last cached copy when offline
    event.respondWith(
      fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
