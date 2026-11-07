"""Views for the map app (Prompt 28).

Two PUBLIC doors, both honest-by-construction:

  * map:route      GET /map/                — the ONE live-map page: Leaflet +
    OSM raster tiles (no Google in V1), markers whose Confidence class is
    visible, and an explicit empty state. `?route=<id>` scopes the page to a
    route (its vehicles' live sessions + that route's stage reports).

  * map:positions  GET /map/positions/      — the SAME payload as JSON for
    the (PWA-cached) page shell: sessions + crowd entries + anchors + empty.

RULES made structural:
  * positions/sessions/overlays come ONLY through map/serialization.py, whose
    sole sources are Prompt 27's and Prompt 30's public doors — no raw
    TrackingSession/PingPoint/CrowdReport manager anywhere in this app;
  * crowd entries render type_label/display AS-IS (30's frozen contract);
  * the empty state is a real template state driven by the payload's empty
    flag — no placeholder marker is ever spun up for absent data;
  * no login gate: the passenger map is public data (27 already guarantees
    positions are invisible off-session at the query layer).
"""
from __future__ import annotations

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import serialization
from . import tiling


def _route_or_none_or_404(route_param: str | None):
    """''/absent → None (citywide); a value that names no route → 404.

    WHY 404 and not an empty map: a route id that doesn't exist is a broken
    link (the honest map NEVER substitutes "some other route" for it).
    """
    if route_param is None or route_param == "":
        return None
    from apps.routes.models import Route  # lazy: registry order

    route = Route.objects.filter(pk=route_param).first() if route_param.isdigit() else None
    if route is None:
        raise Http404(_("No such route."))
    return route


def route_map(request):
    """The live-map page. Payload → json_script data island; the ONLY
    client JS renders the payload's markers (no invention beyond that)."""
    route = _route_or_none_or_404(request.GET.get("route"))
    payload = serialization.route_positions_payload(route, now=timezone.now())
    anchor_ids = {anchor["stage_id"] for anchor in payload["crowd"]["anchors"]}
    crowd_extra = [
        row for row in payload["crowd"]["entries"] if row["stage_id"] not in anchor_ids
    ]
    context = {
        "route": route,
        "payload": payload,
        "empty": payload["empty"],
        "has_route": route is not None,
        "live_vehicle_count": sum(
            1 for entry in payload["sessions"] if entry["positions"]
        ),
        "crowd_count": len(payload["crowd"]["entries"]),
        "crowd_extra": crowd_extra,
        "tiles_config": tiling.tiles_config(),
        "tile_budget_note": tiling.tile_budget_note(),
        "leaflet_css_url": tiling.LEAFLET_CSS_URL,
        "leaflet_css_integrity": tiling.LEAFLET_CSS_INTEGRITY,
        "leaflet_js_url": tiling.LEAFLET_JS_URL,
        "leaflet_js_integrity": tiling.LEAFLET_JS_INTEGRITY,
    }
    return render(request, "map/map.html", context)


def route_positions(request):
    """The map feed as JSON. Same serializer as the page — the payload the
    page renders is EXACTLY what /map/positions/ returns (no drift)."""
    route = _route_or_none_or_404(request.GET.get("route"))
    payload = serialization.route_positions_payload(route, now=timezone.now())
    return JsonResponse(payload)
