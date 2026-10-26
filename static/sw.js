/* Mtaani Connect service worker (PROMPTS.md Prompt 05).
   HARD RULES honored here:
   - never cache POSTs or any non-GET request (auth mutations, API writes)
   - never cache anything under /accounts/ (auth pages)
   - navigation requests: network-first with honest offline fallback — never
     a stale page pretending to be fresh
   - static assets: cache-first under a VERSIONED cache name

   PITFALL (PROMPTS.md): bump CACHE_VERSION on EVERY static asset change,
   or users keep seeing stale pages for days.
*/
var CACHE_VERSION = "mtaani-v0.5.0";
var SHELL_CACHE = "mtaani-shell-" + CACHE_VERSION;
var PRECACHE = [
  "/static/css/mtaani.css",
  "/static/js/app.js",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/offline/"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) { return cache.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys
        .filter(function (k) { return k !== SHELL_CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") { return; }            /* never non-GET */
  var url = new URL(request.url);
  if (url.origin !== location.origin) { return; }     /* same-origin only */
  if (url.pathname.indexOf("/accounts/") === 0) { return; }  /* auth never cached */

  if (request.mode === "navigate") {
    /* network-first, honest fallback when the network is gone */
    event.respondWith(
      fetch(request).catch(function () { return caches.match("/offline/"); })
    );
    return;
  }
  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(
      caches.match(request).then(function (hit) { return hit || fetch(request); })
    );
  }
});
