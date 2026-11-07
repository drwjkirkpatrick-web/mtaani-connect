"""serialization.py — the Prompt 28 live-map payload over PUBLIC doors ONLY.

WHAT this builds: the whole map state for ONE request — live sessions (27's
shape) + stage-anchored crowd overlays (30's frozen entries) + the honest
empty flag. The map app OWNS no transport data; every value below crosses a
tested door:

  * 27 doors (privacy query layer): serialization.live_map_payload /
    serialization.session_positions / services.live_session_for /
    services.visible_positions. The TrackingSession/PingPoint tables are
    NEVER queried directly here (map = a viewer over 27's truth). Why the
    per-session re-derivation: 27's frozen session_positions hardcodes its
    position-layer LIVE_GPS label — its own docstring reserved the key for
    "28's ingest layer own column/label" — so the map renders 27's dicts
    AS-IS and swaps ONLY the confidence VALUES to the Prompt 28 column
    (via the SAME door, same rules, same rows: visible_positions). Honest
    labels: a downgraded ping carries ESTIMATE into the payload; nothing is
    silently upgraded. Cost: one extra privacy-door call per live session —
    NOTE for the parent: folding the column into 27's position_payload is
    the one-query fix for whoever owns that file next wave.

  * 30 SEAM (frozen contract): crowd_overlay_for_stage(stage, now=, scorer=)
    entries are carried AS-IS — same keys (display/type_label/count/…), no
    position keys, confidence = COMMUNITY_REPORT. Because 30's entries carry
    NO coordinates BY CONTRACT, this module delivers a SEPARATE anchors list
    (stage_id → the Stage's own geometry off routes.Route.stage_order, the
    route network's public data) and the map pins crowd reports at STAGE
    geometry only. Never a synthetic position; never a nudged pin.

  * scorer: crowd services.make_report_scorer() in production; injectable
    per 30's contract (pure read path — clock and scorer are parameters).

SHAPES (asserted by tests/test_map.py):
  route_positions_payload -> {route_id, route_name, sessions, crowd,
                              empty, as_of}
  session entries  = EXACTLY 27's key set (frozen in 27's own tests);
  position entries = EXACTLY 27's key set, confidence from the 28 column;
  crowd entries    = EXACTLY 30's returned dicts, unmodified.

DPA/DPIA: pings are high-risk processing — this module never widens what 27
exposes and emits no crew identity (27's bait-scan carries over to map tests).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from apps.crowd import serialization as crowd_serialization
from apps.crowd import services as crowd_services
from apps.tracking import serialization as tracking_serialization
from apps.tracking import services as tracking_services


def _session_entry(session, *, now: datetime) -> dict | None:
    """One vehicle's LIVE session as a 27-shaped entry with honest labels.

    Door chain, all Prompt 27 PUBLIC (nothing else touchable): the vehicle's
    live_session_for instance → session_positions (frozen shape, its OWN
    visible_positions sweep) → one more visible_positions pass to read the
    Prompt 28 per-ping confidence COLUMN → value-swap only.
    """
    entry = tracking_serialization.session_positions(session, now=now)
    column = {
        ping.recorded_at.isoformat(): ping.confidence
        for ping in tracking_services.visible_positions(session, now=now)
    }
    for position in entry["positions"]:
        label = column.get(position["recorded_at"])
        if label is not None:  # column truth wins — never silently upgraded
            position["confidence"] = label
    return entry


def live_sessions_payload(route, *, now: datetime) -> list[dict]:
    """LIVE session entries (27's exact shape) for this request's context.

    route=None → the whole network. Vehicle→route resolution is the route
    network's OWN public data (routes.Route.vehicles — the same related set
    the route detail page serves); no tracking-internal tables anywhere.
    """
    if route is not None:
        from apps.fleet.models import VehicleStatus  # lazy: registry order

        vehicles = route.vehicles.filter(
            status=VehicleStatus.ACTIVE.value
        ).order_by("plate")
    else:
        from apps.fleet import services as fleet_services  # lazy: registry order

        vehicles = fleet_services.active_vehicles()
    entries = []
    for vehicle in vehicles:
        session = tracking_services.live_session_for(vehicle, now=now)
        if session is None:  # not publishing at `now` — honest silence
            continue
        entries.append(_session_entry(session, now=now))
    return entries


def crowd_overlay_payload(
    route, *, now: datetime, scorer: Callable | None = None
) -> tuple[list[dict], list[dict]]:
    """(entries AS-IS from 30, anchors pinned at known stage geometry).

    route=only: overlays are a per-route surface (the route's stages bound
    the queries); citywide requests carry NO crowd layer and the template
    says the honest why. Stages without coordinates stay UNANCHORED — their
    entries still travel to the template (rendered as text), never invented
    onto the map.
    """
    if route is None:
        return [], []
    scorer = crowd_services.make_report_scorer() if scorer is None else scorer
    entries, anchors, seen = [], [], set()
    for link in route.stage_order:  # Route's PUBLIC ordered link table
        stage = link.stage
        for row in crowd_serialization.crowd_overlay_for_stage(
            stage, now=now, scorer=scorer
        ):
            entries.append(row)  # AS-IS: 30's frozen contract, no edits
        coordinate = stage.coordinate
        if coordinate is not None and stage.pk not in seen:
            seen.add(stage.pk)
            anchors.append(
                {
                    "stage_id": stage.pk,
                    "lat": coordinate[0],
                    "lon": coordinate[1],
                    "stage_name": stage.name,
                }
            )
    return entries, anchors


def route_positions_payload(
    route, *, now: datetime, scorer: Callable | None = None
) -> dict:
    """THE map:positions payload — sessions + crowd + the honest empty flag.

    empty is TRUE only when NOTHING is drawable: no live-session position
    anywhere AND no current crowd entry. `now` always injected.
    """
    sessions = live_sessions_payload(route, now=now)
    crowd_entries, anchors = crowd_overlay_payload(route, now=now, scorer=scorer)
    has_positions = any(entry["positions"] for entry in sessions)
    return {
        "route_id": route.pk if route is not None else None,
        "route_name": route.name_en if route is not None else None,
        "sessions": sessions,
        "crowd": {"entries": crowd_entries, "anchors": anchors},
        "empty": not (has_positions or crowd_entries),
        "as_of": now.isoformat(),
    }
