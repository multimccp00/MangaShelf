/* MangaShelf service worker — offline reading.
 *
 * Only two jobs:
 *  1. Keep the app shell (html/js/css) available offline: network-first, so you
 *     always get the fresh UI when connected; the cached copy is only a fallback.
 *  2. Serve downloaded chapters (page images, cover, series JSON) from the
 *     "ms-offline" cache. That cache is filled ONLY by offline.js on an explicit
 *     download — the worker never stores API responses on its own.
 * Writes (POST) are never touched. */
const SHELL = "ms-shell-v1";
const OFFLINE = "ms-offline";

// Cache key for API URLs: drop the token (it can change) and the display/quality
// params, so a downscaled download still matches the reader's normal request.
function keyOf(url) {
  const u = new URL(url, self.location.href);
  for (const p of ["token", "w", "v"]) u.searchParams.delete(p);
  return u.href;
}

const RE_PAGE = /\/api\/page$/;
const RE_COVER = /\/api\/series\/\d+\/cover$/;
const RE_SERIES = /\/api\/series\/\d+$/;

self.addEventListener("install", (e) => { self.skipWaiting(); });

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) {
      if (k.startsWith("ms-shell-") && k !== SHELL) await caches.delete(k);
    }
    await self.clients.claim();
  })());
});

// Network first with a timeout, then cache. `store` decides whether a good
// response is written back (shell: always; series JSON: only if downloaded).
async function networkFirst(req, cacheName, key, storeAlways) {
  const cache = await caches.open(cacheName);
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 4000);
    const res = await fetch(req, { signal: ctl.signal });
    clearTimeout(t);
    if (res.ok && (storeAlways || (await cache.match(key)))) cache.put(key, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(key);
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // fonts etc. -> browser default

  const path = url.pathname;
  if (RE_PAGE.test(path) || RE_COVER.test(path)) {
    e.respondWith((async () => {
      const hit = await (await caches.open(OFFLINE)).match(keyOf(req.url));
      return hit || fetch(req);
    })());
  } else if (RE_SERIES.test(path)) {
    e.respondWith(networkFirst(req, OFFLINE, keyOf(req.url), false));
  } else if (!path.includes("/api/")) {
    // App shell: navigations and static files. Key by URL minus query for
    // navigations (start_url carries ?pwa=...), full URL for versioned assets.
    const isDoc = req.mode === "navigate" || path.endsWith("/") || path.endsWith(".html");
    const key = isDoc ? url.origin + path : req.url;
    e.respondWith(networkFirst(req, SHELL, key, true));
  }
});
