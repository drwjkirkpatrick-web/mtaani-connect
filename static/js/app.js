/* Mtaani Connect — progressive enhancement only (~2 KB).
   WHY tiny: this runs on low-cost Android over 3G. Everything works without it. */
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

  /* PWA: register the service worker. Registration failure is never fatal. */
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/static/sw.js").catch(function () {});
  }
})();
