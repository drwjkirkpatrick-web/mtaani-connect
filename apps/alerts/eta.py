"""eta.py — honest arrival arithmetic (Prompt 31). Pure, clock-injected.

ETA v1 is ARITHMETIC, not ML (spec verbatim): rolling speed from recent
pings × remaining route distance, labelled ``Confidence.ESTIMATE``, shown
as a RANGE (±25 % band speeds) and SILENT when history is insufficient.

HONESTY CONTRACT (the pitfall, enforced in code):

  * An estimate NEVER renders without its ESTIMATE label — the label is a
    key INSIDE estimate_payload(), so a template or JSON consumer cannot
    render one without the other.
  * A missing estimate NEVER renders as infinity or 0 — every insufficient
    input path (speed None/0, too few fixes, no visible pings, stage with
    no coordinates) returns None, and the caller displays the status
    strings below instead of a number.

DATA DISCIPLINE: positions enter ONLY through Prompt 27's door
(apps.tracking.services.visible_positions) — never a raw ping manager
query. rolling_speed() itself is PURE (takes the ping list the caller
already obtained through the door).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils.translation import gettext_lazy as _

from apps.core.geo import distance_km
from apps.core.types import Confidence

# ---------------------------------------------------------------- constants
# The rolling-speed window: pings older than this do not count as "recent".
SPEED_WINDOW_MIN = 10
# The visible range spread (spec: ±25 % band — "5–12 min" is 24 min/13 min-ish):
# hi = distance / (speed × (1 − band)); lo = distance / (speed × (1 + band)).
SPEED_BAND_FRACTION = 0.25

# Statuses for the SILENT cases — rendered instead of a number, never 0/inf.
MSG_INSUFFICIENT_DATA = _("Not enough live data to estimate.")
MSG_NOT_TRACKING = _("Not tracking right now.")

ETA_LABEL = Confidence.ESTIMATE  # the label that shadows every estimate


# -------------------------------------------------------------------- speed
def rolling_speed(pings, *, now: datetime, window_min: int = SPEED_WINDOW_MIN) -> float | None:
    """Mean IMPLIED speed (km/h) over consecutive fixes inside the window.

    PURE: `pings` is any iterable of position rows (lat/lon/recorded_at) —
    the caller has already fetched them through visible_positions().

    None, honestly, when:
      * fewer than 2 usable fixes inside ``now − window_min``
      * a segment spans zero time or zero distance (stationary or duplicate
        fixes say nothing about motion — speed None means "cannot estimate").
    WHY mean of implied speeds: ping intervals are irregular on 2 G links;
    per-segment implied speeds average out dropouts without pretending
    precision a single ping can't back.
    """
    cutoff = now - timedelta(minutes=window_min)
    points = []
    for ping in sorted(pings, key=lambda p: (p.recorded_at, p.pk)):
        if ping.recorded_at >= cutoff:
            points.append(((float(ping.lat), float(ping.lon)), ping.recorded_at))
    if len(points) < 2:  # < 2 usable fixes — insufficient history
        return None
    speeds = []
    for (point_a, at_a), (point_b, at_b) in zip(points, points[1:]):
        dt_hours = (at_b - at_a).total_seconds() / 3600.0
        if dt_hours <= 0:
            continue  # duplicate/zero-width segment — unusable, not 0-speed
        speeds.append(distance_km(point_a, point_b) / dt_hours)
    usable = [s for s in speeds if s > 0.0]
    if not usable:
        return None
    mean = sum(usable) / len(usable)
    if not (mean > 0.0):  # also filters NaN/negative nonsense
        return None
    return mean


# ---------------------------------------------------------------------- ETA
def eta_minutes_from(remaining_km: float, speed_kmh: float) -> tuple[float, float] | None:
    """(lo_min, hi_min) pure arithmetic — the core the tests pin exactly.

    ± SPEED_BAND_FRACTION speed band → a VISIBLE RANGE, not a fake-precise
    number. Guards: speed ≤ 0 → None (a zero speed must never divide —
    that is the infinity trap); non-finite results → None.
    """
    if remaining_km is None or speed_kmh is None:
        return None
    if speed_kmh <= 0.0:
        return None
    lo_min = (remaining_km / (speed_kmh * (1.0 + SPEED_BAND_FRACTION))) * 60.0
    hi_min = (remaining_km / (speed_kmh * (1.0 - SPEED_BAND_FRACTION))) * 60.0
    if not (lo_min >= 0.0 and hi_min >= 0.0) or lo_min > hi_min:
        return None
    # NOTE: lo/hi == 0.0 when the vehicle is at the stage — that is real data
    # ("any minute now"), not a missing estimate. MISSING stays None above.
    return (lo_min, hi_min)


def last_visible_ping(session, *, now: datetime):
    """The LAST position a reader may see — through 27's door ONLY.

    WHY this wrapper: it is the single point that keeps eta/approach logic
    from ever touching a raw PingPoint manager (the pitfall made structural).
    An empty window (not live / expired / fully suppressed) → None.
    """
    from apps.tracking import services as tracking_services  # lazy: registry order

    return tracking_services.visible_positions(session, now=now).last()


def eta_minutes(session, stage, *, now: datetime) -> "tuple[float, float] | None":
    """(lo_min, hi_min) for `session`'s vehicle → `stage`, or None.

    remaining distance = stage coordinate ↔ LAST VISIBLE ping (through the
    door); speed = rolling_speed() over the same visible window. ANY missing
    ingredient (no visible pings, speed insufficient, stage has no
    coordinates at all) → None — the caller shows MSG_INSUFFICIENT_DATA or
    MSG_NOT_TRACKING, never 0 or ∞.
    """
    if stage is None or stage.coordinate is None:
        return None  # a stage with no coordinates can never be honest arithmetic
    from apps.tracking import services as tracking_services  # lazy: registry order

    pings = list(tracking_services.visible_positions(session, now=now))
    if not pings:
        return None
    last = pings[-1]
    remaining_km = distance_km((float(last.lat), float(last.lon)), stage.coordinate)
    speed_kmh = rolling_speed(pings, now=now)
    if speed_kmh is None:
        return None  # insufficient history — SILENCE, never 0 or ∞
    return eta_minutes_from(remaining_km, speed_kmh)


def estimate_payload(lo_min: float, hi_min: float) -> dict:
    """The ESTIMATE payload — KEY SET IS FROZEN (tests pin it exactly).

    The label lives INSIDE the payload so no surface can render an estimate
    without its ESTIMATE label (the pitfall, structural).
    """
    return {
        "lo_min": float(lo_min),
        "hi_min": float(hi_min),
        "confidence": ETA_LABEL.value,
    }


def format_range(eta: tuple[float, float]) -> str:
    """"(5–12 min" display form: minutes, en-dash, "<1 min" under a minute."""
    lo, hi = max(0.0, float(eta[0])), max(0.0, float(eta[1]))
    if hi < 1.0:
        return _("< 1 min")
    lo_r, hi_r = int(round(lo)), int(round(hi))
    if hi_r == lo_r:
        return _("~%(m)s min") % {"m": lo_r}
    return _("%(lo)s–%(hi)s min") % {"lo": lo_r, "hi": hi_r}


def approaching_stage(
    session, stage, *, radius_km: float | None = None, now: datetime
) -> bool:
    """True ⇔ the vehicle's LAST VISIBLE ping sits within radius of the stage.

    THE ALERT TRIGGER. Boundary-INCLUSIVE (<=, same settled semantics as
    Prompt 19's ST_DWithin ring). Honest False when: no visible pings (not
    tracking / expired / suppressed) or the stage has no coordinates —
    an unknown position is never "approaching" (no fabricated triggers).
    """
    if radius_km is None:
        from .models import DEFAULT_RADIUS_KM  # lazy: model-layer constant
        radius_km = DEFAULT_RADIUS_KM
    if stage is None or stage.coordinate is None:
        return False
    last = last_visible_ping(session, now=now)
    if last is None:
        return False
    km = distance_km((float(last.lat), float(last.lon)), stage.coordinate)
    return km <= float(radius_km)
