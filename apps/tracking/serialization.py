"""serialization.py — the Prompt 28 map seam (shape-stable, tested).

THE 28 SEAM: when the live map lands it calls the functions HERE and
renders the dicts AS-IS (same pattern as crowd.serialization). It must
never build its own position list: the ONLY path from the database to any
position payload runs through services.visible_positions(), so the
suppression rules travel with the data — the pitfall rule made structural.

PRIVACY CONTRACT baked into the shapes (exactly asserted by tests:
"crew identity absence from all public serializations"):

  position payload keys (the FULL set):
      session_id, vehicle_id, lat, lon, recorded_at, confidence
  session payload keys (the FULL set):
      session_id, vehicle_id, plate, nickname, live, started_at, positions

  - NO crew identity of any kind: no authorized_by, no user, no username,
    no role, no token, no token hash — keys absent AND values absent
    (distinctive bait strings are scanned in tests);
  - plate/nickname ARE present: business data, public by design (KDPA
    split — same as fleet's search page);
  - confidence is the per-layer source label (Confidence enum at the
    position layer) — 28's ingest may downgrade per-ping; the label key
    exists NOW so the map shape doesn't change later;
  - an ended/expired session serializes with positions=[] and live=False
    — the honest empty state, never invented history.
"""
from __future__ import annotations

from datetime import datetime

from apps.core.types import Confidence

from . import services
from .models import SessionStatus, TrackingSession


def position_payload(ping, vehicle_id: int | None = None) -> dict:
    """One retained, suppression-passed ping as a map-marker dict.

    vehicle_id: pass the CALLER's session.vehicle_id (session_positions
    does) — otherwise it is read through ping.session (a per-ping query we
    don't want; this argument keeps the seam N+1-free).

    Exact key set asserted in tests — additions mean a shape change that
    would need a privacy re-review, not a quiet key.
    """
    return {
        "session_id": ping.session_id,
        "vehicle_id": vehicle_id if vehicle_id is not None else ping.session.vehicle_id,
        "lat": float(ping.lat),
        "lon": float(ping.lon),
        "recorded_at": ping.recorded_at.isoformat(),
        # Source label at the POSITION layer: session-authorized GPS fixes.
        # Per-ping plausibility downgrades are 28's ingest layer (they keep
        # this key name when they land their own column/label).
        "confidence": Confidence.LIVE_GPS.value,
    }


def session_positions(session, *, now: datetime) -> dict:
    """One session (any state) as the map-ready payload.

    positions come ONLY from services.visible_positions (rules travel with
    the data); live reflects the DERIVED status at `now`.
    """
    positions = services.visible_positions(session, now=now)
    return {
        "session_id": session.pk,
        "vehicle_id": session.vehicle_id,
        "plate": session.vehicle.plate,
        "nickname": session.vehicle.nickname,
        "live": services.session_status(session, now=now)
        == SessionStatus.LIVE.value,
        "started_at": session.started_at.isoformat(),
        "positions": [position_payload(p, session.vehicle_id) for p in positions],
    }


def live_map_payload(*, now: datetime) -> list[dict]:
    """EVERYTHING the map may draw right now: one entry per LIVE session,
    positions only where the privacy rules allow them.

    WHY the caller is this function: the map never queries
    TrackingSession/PingPoint itself — it inherits THIS sweep's gates
    (live-only, unswept-expired excluded) and each entry's suppression
    rules in one call.
    """
    live_rows = [
        row
        for row in TrackingSession.objects.filter(ends_at__isnull=True)
        .select_related("vehicle")
        .order_by("started_at")
        if services.session_status(row, now=now) == SessionStatus.LIVE.value
    ]
    return [session_positions(row, now=now) for row in live_rows]
