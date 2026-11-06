/* Mtaani Connect — progressive enhancement only (~4 KB).
   WHY tiny: this runs on low-cost Android over 3G. Everything is usable
   without it; the service worker (static/sw.js) does the heavy caching. */
(function () {
  "use strict";

  var banner = document.getElementById("offline-banner");
  if (banner) {
    banner.hidden = navigator.onLine;
    window.addEventListener("online", function () { banner.hidden = true; });
    window.addEventListener("offline", function () { banner.hidden = false; });
  }

  /* fetch-fail listener: flips the offline banner even when the OS lies. */
  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function () {
      return nativeFetch.apply(this, arguments).catch(function (err) {
        if (banner) { banner.hidden = false; }
        throw err;
      });
    };
  }

  /* PWA: register the service worker. Registration failure is never fatal,
     and without a controlling SW the page still works plain. */
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js").catch(function () {});
  }

  /* Prompt 09 — offline saved guides. The save/unsave forms POST to JSON
     endpoints; with JS we intercept and forward the payload + hash to the
     service worker so caching is instant (contract: apps/guides/offline.py).
     Without JS the server still records the save/unsave — nothing breaks,
     the SW reconciles everything at the next manifest sync anyway. */
  function csrfToken() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }

  function tellSW(message) {
    if (!("serviceWorker" in navigator) || !navigator.serviceWorker.controller) return;
    navigator.serviceWorker.controller.postMessage(message);
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches("form[data-offline-json]")) { return; }
    event.preventDefault();
    var button = form.querySelector("button");
    if (button) { button.disabled = true; }
    fetch(form.getAttribute("action"), {
      method: "POST",
      headers: { "X-CSRFToken": csrfToken() },
      body: new FormData(form)
    }).then(function (resp) {
      if (resp.ok) { return resp.json(); }
      throw new Error("save failed");
    }).then(function (data) {
      if (data.saved) {
        /* payload = the page, its photos, and the revision hash the SW
           compares at the next sync (hash, not dates — clocks lie). */
        tellSW({ type: "OFFLINE_SAVE", url: data.url, hash: data.hash,
                 images: data.images || [] });
      } else {
        tellSW({ type: "OFFLINE_UNSAVE", url: data.url });
      }
      if (button) { button.disabled = false; }
      location.reload();
    }).catch(function () {
      if (button) { button.disabled = false; }
      /* let the plain POST fall through so feedback still surfaces */
      form.submit();
    });
  });

  /* Boot-time manifest reconciliation, throttled to 30 min: re-sync happens
     ONLY on the SW side via revision hashes (apps/guides/offline.py), so
     unchanged guides re-download nothing. */
  try {
    var last = parseInt(localStorage.getItem("mtaani-last-sw-sync") || "0", 10);
    if (Date.now() - last > 30 * 60 * 1000) {
      localStorage.setItem("mtaani-last-sw-sync", String(Date.now()));
      tellSW({ type: "OFFLINE_SYNC" });
    }
  } catch (e) { /* storage off: the SW still syncs on activate */ }
})();
