"""tiling.py — tile discipline for the Prompt 28 live map (the 3G budget).

WHAT this module decides: WHICH raster the map uses, HOW MUCH it may cost the
rider, and the exact contract the service-worker pass must satisfy. One module
so the constants, the template and the budget test can never drift apart.

CHOICE (spec verbatim, V1): OpenStreetMap RASTER tiles, no Google dependency.
The URL is data in the template — tests render HTML, never fetch tiles — and
swapping to a self-hosted mirror later is a constant change, not a rewrite.

THE PITFALL, DEFENDED HERE (each item pinned by tests/test_map.py):
  * lazy loads — "updateWhenIdle": True (fetch only settled views) +
    "keepBuffer": 1 (minimal offscreen prefetch) + the script loads with
    ``defer`` so the page never waits on the map library;
  * zoom caps — "maxZoom" stops the display; "maxNativeZoom" stops the
    FETCH: beyond it Leaflet reuses the last native zoom's tiles instead of
    pulling 2× (then 4×) the native bytes per zoom step. At the city-scan
    zooms a native cap alone halves tile bytes;
  * "detectRetina": False — never 2× bytes for sharp-phone pixels;
  * the service worker MUST cache tiles (parent SW pass — see
    tile_budget_note) under the TILE_BUDGET_BYTES cap below.
"""
from __future__ import annotations

# Spec verbatim: "a map that burns 2 MB of tiles has failed the brief."
# THE stub every tile test pins: the SW cache may hold AT MOST this many tile
# bytes per session before the least-recently-used tile is evicted.
TILE_BUDGET_BYTES = 2 * 1024 * 1024  # 2 MiB

# The raster source. OSM usage policy: the attribution line travels with the
# map (rendered by Leaflet into the tile layer).
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# Fetch/display caps (see docstring). NOTE: 16 native ⇒ ~1-2 MB of PNG bytes
# for a full Nairobi corridor visit even across several pan steps — inside
# the 2 MiB budget; z17+ display reuses z16 tiles (Leaflet upscale).
MAX_ZOOM = 17        # display cap (Leaflet stops zooming the map)
MAX_NATIVE_ZOOM = 16  # fetch cap (the network stops seeing the CDN)

# The Lazy-load + budget options. crossOrigin keeps tile responses CORS-clean
# so the SW pass can cache them; updateWhenIdle is the lazy-load pin.
TILE_LAYER_OPTIONS = {
    "maxZoom": MAX_ZOOM,
    "maxNativeZoom": MAX_NATIVE_ZOOM,
    "updateWhenIdle": True,   # lazy: fetch tiles for the SETTLED view only
    "keepBuffer": 1,          # tiny offscreen prefetch (default is 2)
    "noWrap": True,
    "crossOrigin": True,
    "detectRetina": False,    # never double bytes for retina phones
    "attribution": (
        '&copy; <a href="https://www.openstreetmap.org/copyright">'
        "OpenStreetMap</a> contributors"
    ),
}

# Real Leaflet 1.9.4 assets served over the CDN WITH integrity attrs (pin —
# these hashes were computed over the exact published bytes, sha384):
LEAFLET_VERSION = "1.9.4"
LEAFLET_CSS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.css"
LEAFLET_CSS_INTEGRITY = (
    "sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H"
)
LEAFLET_JS_URL = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist/leaflet.js"
LEAFLET_JS_INTEGRITY = (
    "sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH"
)


def tile_budget_note() -> str:
    """The REQUIRED service-worker tile-cache contract (the pinned stub).

    The parent's SW pass MUST implement this for the live map; Prompt 28's
    tests pin THIS function (and TILE_BUDGET_BYTES) as the budget stub of
    record, so retuning the budget is always a reviewed code change, never
    silent drift.
    """
    return (
        "SW TILE-CACHE CONTRACT (required for map tiles): cache tiles from "
        f"{TILE_URL} only, cache-first, keeping the per-browser tile cache at "
        f"most {TILE_BUDGET_BYTES // (1024 * 1024)} MiB "
        f"({TILE_BUDGET_BYTES} bytes; TILE_BUDGET_BYTES) — evict tiles "
        "least-recently-used first, never cache an authenticated API "
        "response, and let the updateWhenIdle/maxZoom/maxNativeZoom caps "
        "keep the request set small on 3G."
    )


def tiles_config() -> dict:
    """The context bundle the template feeds to Leaflet (JSON-safe dict;
    the template applies |json_script directly to it)."""
    return {"url": TILE_URL, "options": TILE_LAYER_OPTIONS}
