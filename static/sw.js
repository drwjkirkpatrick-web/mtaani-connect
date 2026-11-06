/* Mtaani Connect service worker (PROMPTS.md Prompt 05 + Prompt 09).
   HARD RULES honored here:
   - never cache POSTs or any non-GET request (auth mutations, API writes)
   - never cache anything under /accounts/ (auth pages)
   - navigation requests: network-first with honest offline fallback — never
     a stale page pretending to be fresh
   - static assets: cache-first under a VERSIONED cache name

   PITFALL (PROMPTS.md): bump CACHE_VERSION on EVERY static asset change,
   or users keep seeing stale pages for days.

   OFFLINE SAVED GUIDES (Prompt 09, parent-owned glue). Authoritative source:
   GET /guides/offline-manifest.json (paginated; behavior contract in
   apps/guides/offline.py). Re-sync compares REVISION HASHES — names are
   dates never, re-downloading identical content burns bundles:
     stored hash == manifest hash  → skip (identical content)
     stored missing/different      → fetch page + images, record hash
     manifest hash null (no_data)  → skip — keep serving the stale copy
                                     offline; never revalidate nothing
   Anything absent from the manifest is implicitly unsaved → evicted.
*/
var CACHE_VERSION = "mtaani-v0.6.0";
var SHELL_CACHE = "mtaani-shell-" + CACHE_VERSION;
var GUIDE_CACHE = "mtaani-guides-" + CACHE_VERSION;
var MANIFEST_PATH = "/guides/offline-manifest.json";
/* Cache-only registry — NEVER fetched over the network. Keyed by guide page
   URL: {h: revision hash, i: [image URLs]}. Written wholesale from the
   manifest after each successful sync pass. */
var HASH_KEY = "/guides/offline-hashes.json";
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
        /* keep BOTH caches of this version (shell + offline guides) */
        .filter(function (k) { return k !== SHELL_CACHE && k !== GUIDE_CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
      .then(function () { return syncOfflineGuides(); })
      .catch(function () { /* manifest sync is best-effort at boot */ })
  );
});

/* ---------- offline-guides registry helpers ---------- */

function readHashes(cache) {
  return cache.match(HASH_KEY).then(function (hit) {
    return hit ? hit.json() : {};
  });
}

function writeHashes(cache, hashes) {
  return cache.put(HASH_KEY, new Response(JSON.stringify(hashes), {
    headers: { "Content-Type": "application/json" }
  }));
}

/* Fetch one guide page + its images into the open cache. WHY page-first:
   a page that fails to fetch stays un-cached; images are best-effort so a
   flaky network leaves a readable guide with zero steps of it missing. */
function cacheGuidePageInto(cache, pageUrl, images) {
  function grab(url) {
    return fetch(new Request(url, { credentials: "same-origin" })).then(function (resp) {
      if (resp && resp.ok) { return cache.put(url, resp).catch(function () {}); }
    }).catch(function () { /* offline mid-save: leave un-cached, retry later */ });
  }
  return cache.match(pageUrl).then(function (already) {
    if (already) { return true; }   /* identical content never re-downloaded */
    return grab(pageUrl).then(function () {
      return cache.match(pageUrl);  /* did the page actually land? */
    }).then(function (hit) {
      return Promise.all((images || []).map(grab)).then(function () {
        return !!hit;
      });
    });
  });
}

/* Unsave: evict the page + the images the registry recorded. The unsave
   response carries no image list — the registry does. */
function forgetGuidePage(pageUrl) {
  return caches.open(GUIDE_CACHE).then(function (cache) {
    return readHashes(cache).then(function (hashes) {
      var entry = hashes[pageUrl];
      var doomed = [pageUrl].concat(entry && entry.i ? entry.i : []);
      delete hashes[pageUrl];
      return writeHashes(cache, hashes).then(function () {
        return Promise.all(doomed.map(function (u) { return cache.delete(u); }));
      }).then(function () {
        return sweepAgainst(cache, hashes); /* registry now = source of truth */
      });
    });
  });
}

/* The manifest is authoritative: cached guide pages/images absent from the
   keep-list are dropped (server-side unsave may have happened elsewhere). */
function sweepAgainst(cache, keep) {
  keep = Object.assign({}, keep);
  keep[HASH_KEY] = true;
  keep["/guides/offline/"] = true;
  keep["/guides/offline"] = true;
  return cache.keys().then(function (requests) {
    var ops = [];
    requests.forEach(function (req) {
      var u = new URL(req.url);
      if (u.pathname.indexOf("offline-manifest.json") !== -1) { return; }
      if (req.url === HASH_KEY) { return; }
      var pathUrl = u.pathname + u.search;
      if (keep[req.url] || keep[pathUrl]) { return; }
      ops.push(cache.delete(req));
    });
    return Promise.all(ops);
  });
}

/* Manifest-walk sync. Hash comparison ONLY — never dates (clocks lie). */
function syncOfflineGuides() {
  var fresh = {};  /* final registry: url -> {h, i}, built from the manifest */

  function walk(cache, page) {
    return fetch(MANIFEST_PATH + "?page=" + page, { credentials: "same-origin" })
      .then(function (resp) {
        if (!resp || !resp.ok) { return null; }
        var copy = resp.clone();
        return resp.json().then(function (data) {
          return cache.put(MANIFEST_PATH + "?page=" + page, copy).then(function () {
            return readHashes(cache).then(function (hashes) {
              return Promise.all((data.guides || []).map(function (g) {
                var stored = hashes[g.url];
                if (g.hash === null) { return Promise.resolve(); }  /* no_data */
                if (stored && stored.h === g.hash) {
                  fresh[g.url] = stored;   /* unchanged: keep old record */
                  return Promise.resolve();
                }
                return cacheGuidePageInto(cache, g.url, g.images || [])
                  .then(function (pageOk) {
                    if (pageOk) {
                      fresh[g.url] = { h: g.hash, i: g.images || [] };
                    } else if (stored) {
                      fresh[g.url] = stored;  /* failed fetch: retry next sync */
                    }
                  });
              }));
            });
          }).then(function () {
            if (page < (data.pages || 1)) { return walk(cache, page + 1); }
          });
        });
      })
      .catch(function () { return null; }); /* network gone: keep what we have */
  }

  return caches.open(GUIDE_CACHE).then(function (cache) {
    return walk(cache, 1).then(function () {
      return writeHashes(cache, fresh);   /* registry = final manifest state */
    }).then(function () {
      /* protect unchanged+failed entries too: sweep keeps ALL manifest urls */
      return cache.keys().then(function () {
        var manifestKeeps = {};
        Object.keys(fresh).forEach(function (u) { manifestKeeps[u] = true; });
        return sweepAgainst(cache, manifestKeeps);
      });
    });
  });
}

/* ---------- messages from app.js ---------- */

self.addEventListener("message", function (event) {
  var msg = event.data || {};
  if (msg.type === "OFFLINE_SAVE") {
    /* immediate cache of the just-saved page + images; no manifest walk */
    event.waitUntil(
      caches.open(GUIDE_CACHE).then(function (cache) {
        return readHashes(cache).then(function (hashes) {
          hashes[msg.url] = { h: msg.hash, i: msg.images || [] };
          return writeHashes(cache, hashes);
        }).then(function () {
          return cacheGuidePageInto(cache, msg.url, msg.images || []);
        });
      })
    );
  } else if (msg.type === "OFFLINE_UNSAVE") {
    event.waitUntil(forgetGuidePage(msg.url));
  } else if (msg.type === "OFFLINE_SYNC") {
    event.waitUntil(syncOfflineGuides());
  }
});

/* ---------- fetch strategy ---------- */

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") { return; }            /* never non-GET */
  var url = new URL(request.url);
  if (url.origin !== location.origin) { return; }     /* same-origin only */
  if (url.pathname.indexOf("/accounts/") === 0) { return; }  /* auth never cached */

  /* manifest: network-first, cached manifest page as the offline fallback */
  if (url.pathname === MANIFEST_PATH) {
    event.respondWith(
      fetch(request).catch(function () {
        return caches.open(GUIDE_CACHE).then(function (cache) {
          return cache.match(request.url).then(function (hit) {
            return hit || Response.error();
          });
        });
      })
    );
    return;
  }

  /* the offline list page ITSELF must open with zero connectivity:
     cache-first navigation with a quiet background refresh */
  if (url.pathname === "/guides/offline/" || url.pathname === "/guides/offline") {
    event.respondWith(
      caches.open(GUIDE_CACHE).then(function (cache) {
        return cache.match(request).then(function (hit) {
          var refresh = fetch(request).then(function (resp) {
            if (resp && resp.ok) { return cache.put(request, resp.clone()); }
          }).catch(function () { return null; }); /* offline: cache already won */
          event.waitUntil(refresh);
          return hit || fetch(request);
        });
      })
    );
    return;
  }

  if (request.mode === "navigate") {
    /* network-first, honest fallback: the saved guide's own cached copy
       (airplane mode opens the guide WITH steps), else the offline page */
    event.respondWith(
      fetch(request).catch(function () {
        return caches.match(request, { cacheName: GUIDE_CACHE }).then(function (hit) {
          return hit || caches.match("/offline/");
        });
      })
    );
    return;
  }

  /* media for saved guides: cache-first through the guide cache. Images not
     saved offline fall straight through to the network, unchanged. */
  if (url.pathname.indexOf("/media/") === 0) {
    event.respondWith(
      caches.match(request, { cacheName: GUIDE_CACHE }).then(function (hit) {
        return hit || fetch(request);
      })
    );
    return;
  }

  if (url.pathname.indexOf("/static/") === 0) {
    event.respondWith(
      caches.match(request).then(function (hit) { return hit || fetch(request); })
    );
  }
});
