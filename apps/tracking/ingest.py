"""ingest.py — Prompt 28: batched ping ingestion + its token-auth view.

CONTRACT FOR PROMPT 29 (the crew device — implement THIS, nothing else):
    POST /tracking/ingest/<int:session_id>/       (route name: tracking:ingest)
    Header:  X-Tracking-Token: <raw token>       (the display-once "mtk_…"
              string shown at session start/rotation)
    Body (application/json):
        {
          "vehicle_id": <int>,          # the vehicle the token is scoped to
          "pings": [                    # AT MOST INGEST_MAX_BATCH (50) items
            {"recorded_at": "2026-01-15T10:01:00Z",   # ISO-8601 device stamp
             "lat": -1.28640, "lon": 36.81720,        # WGS84 decimal degrees
             "speed_kmh": 41.0}                       # OPTIONAL / nullable
          ],
        }
    Flush cadence: every 30 s–2 min, NOT per-fix (battery + data — spec).
    Responses (always JSON):
      200 {"accepted": N, "downgraded": N, "duplicates": N,
           "batch_limit": 50, "session_id": <id>, "vehicle_id": <id>}
          accepted + downgraded + duplicates == len(pings) — the honest
          arithmetic the tests pin.
      400 {"accepted": 0, "downgraded": 0, "duplicates": 0,
           "error": <plain reason>}; body is malformed JSON, an item is
          malformed, the batch is over-limit, or the session is no longer
          live (plain wording, no credential language).
      403 the SAME {"accepted": 0, …, "error": <ONE uniform message>} for
          EVERY failed credential — missing header, wrong token, wrong
          vehicle_id, nonexistent session. Reveals NOTHING about whether
          the session exists; never 500.
    Not login-gated (cookies are useless to a background device) but ALWAYS
    token-authenticated: the client IS the crew device.

AUTH CHAIN — Prompt 27's public doors ONLY, in this order:
    1. services.verify_session_token(raw_token, session_id=, vehicle_id=)
       → the session row or None (scope-baked digest: a captured token
       cannot be replayed against another session/vehicle).
    2. services.session_status(session, now=now) must be "LIVE" — the token
       authenticates; the deadline rule authorizes (an expired-but-unswept
       row is refused identically to a swept one).

DPIA NOTE (KDPA 2019 — this endpoint is the WRITE side of high-risk
processing; apps/tracking/services.py IS the read-side mitigation):

  * ingest only ADDS honest rows — it never widens visibility; every read
    (the map, sweeps, Prompt 29's views) still runs through 27's query
    layer;
  * the dedupe key (session, recorded_at) is ModelPrompt 27's unique floor;
    dedupe BEFORE insert keeps late/offline replays from double-counting
    one fix;
  * the plausibility review DOWNGRADES (row kept, labeled ESTIMATE) and
    never deletes — deleting a suspicious fix would erase where the bus was
    seen; the map instead renders its ESTIMATE class (dashed ring) so a
    rider can see the signal is unverified;
  * no crew identity travels on this endpoint or any payload built from it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt

from apps.core.geo import distance_km
from apps.core.types import Confidence

from . import services
from .models import PingPoint, SessionStatus

# ------------------------------------------------------------------ constants
INGEST_MAX_BATCH = 50  # spec verbatim: batched upload, ≤50 pings per request

# Plausibility ceiling (spec verbatim: speed > 150 km/h → downgrade, not
# deletion). Applied to BOTH the device-reported speed and the speed IMPLIED
# by the distance/time between consecutive pings — a GPS fix can jump many
# kilometres while the device reports a small speed; the implied check is
# what catches exactly that.
IMPOSSIBLE_SPEED_KMH = 150.0

# Defensive body cap: 50 pings × ~120 bytes ≈ 6 KB; a request near this cap
# is buggy or hostile — honest 400, never a memory sink.
MAX_BODY_BYTES = 200_000

INGEST_HEADER = "X-Tracking-Token"  # THE header name (pinned by tests)

# ------------------------------------------------------------------- messages
# ONE uniform credential refusal — SessionAuthRefused everywhere:
MSG_AUTH_REFUSED = _(
    "Refused: the tracking credentials on this request are not valid."
)
# Plain wording (NOT credential language) for a verified-but-closed window:
MSG_PUBLISHING_OVER = _(
    "This trip is not publishing live any more — its session ended or "
    "reached its cap. Start a new session before uploading pings."
)
MSG_BAD_BATCH = _(
    'Body expects {"vehicle_id": <int>, "pings": [<positions>]}.'
)
MSG_BATCH_TOO_LARGE = _(
    "At most %(limit)s pings per request — split the flush and send the "
    "rest next round (batch every 30 s–2 min instead of per fix)."
)
MSG_BAD_ITEM = _(
    "Ping at index %(index)s is malformed: it needs recorded_at (ISO-8601), "
    "lat and lon numbers."
)
MSG_ITEM_OUT_OF_RANGE = _(
    "Ping at index %(index)s has out-of-range coordinates — lat within "
    "±90, lon within ±180."
)


class SessionAuthRefused(ValidationError):
    """The UNIFORM credential refusal (missing/bad token, wrong scope).

    WHY a distinct subclass: the view answers EVERY member with the SAME
    status+body regardless of HOW the credential failed — an ingest
    endpoint must never become an existence oracle for sessions/vehicles.
    """


def _now(now: datetime | None) -> datetime:
    """Injected-clock default (every sibling sweep's convention)."""
    return timezone.now() if now is None else now


# -------------------------------------------------------------------- parsing
def implied_speed_kmh(
    prev_moment: datetime | None,
    prev_point: tuple[float, float] | None,
    moment: datetime,
    point: tuple[float, float],
) -> float | None:
    """km/h implied by one fix's distance/time from its predecessor — PURE.

    (lat, lon) tuples, human order; distance is apps.core.geo's haversine.
    None when there is NO predecessor, or time does not move forward: a
    backward/zero-time hop can only manufacture a false verdict, so the
    honest review simply cannot judge it and keeps the row live-pending.
    """
    if prev_moment is None or prev_point is None or not moment > prev_moment:
        return None
    hours = (moment - prev_moment).total_seconds() / 3600.0
    return distance_km(prev_point, point) / hours


def _to_decimal_6(raw) -> Decimal:
    """A JSON number/string → Decimal at the column's 6 dp."""
    return Decimal(str(raw)).quantize(Decimal("0.000001"))


def _parse_recorded_at(value) -> datetime:
    """ISO-8601 stamp → aware UTC datetime.

    A tz-naive stamp is READ AS UTC: crew devices stamp UTC, and refusing
    offline-queued fixes over a missing 'Z' would punish exactly the
    low-data batching the brief wants. Clock skew is lived with, not
    "corrected" — recorded_at stays the device's own stamp.
    """
    moment = datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment.astimezone(dt_timezone.utc)


def _parse_speed(raw) -> Decimal | None:
    """speed_kmh is blankable: absent/null/''/non-numeric → None (unknown).

    WHY None and not 0.0: an unknown speed is the ABSENCE of a claim — a
    0.0 would itself be a number the map might later trust.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    try:
        speed = float(str(raw))
    except (TypeError, ValueError):
        return None
    if speed < 0:  # negative "speeds" are device noise, not a signal
        return None
    return Decimal(str(round(speed, 2)))


def _parse_items(pings) -> list[dict]:
    """Every item → {recorded_at, lat, lon, speed}; malformed → refuse ALL.

    WHY all-or-nothing: the device needs deterministic retry semantics —
    either the whole flush landed or it cleans up and resends. Dropping
    items would make the counts numeric-honest but lie about whether the
    trip's history is complete.
    """
    parsed = []
    for index, item in enumerate(pings):
        if not isinstance(item, dict):
            raise ValidationError(MSG_BAD_ITEM % {"index": index})
        try:
            recorded_at = _parse_recorded_at(item["recorded_at"])
            lat = _to_decimal_6(item["lat"])
            lon = _to_decimal_6(item["lon"])
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            raise ValidationError(MSG_BAD_ITEM % {"index": index})
        if not (-90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0):
            raise ValidationError(MSG_ITEM_OUT_OF_RANGE % {"index": index})
        parsed.append(
            {
                "recorded_at": recorded_at,
                "lat": lat,
                "lon": lon,
                "speed": _parse_speed(item.get("speed_kmh")),
            }
        )
    return parsed


# -------------------------------------------------------------- the service
def ingest_pings(
    raw_token: str | None,
    session_id: int,
    vehicle_id: int | None,
    pings,
    *,
    now: datetime | None = None,
) -> dict:
    """THE ingestion service: 27-auth → LIVE gate → batch cap → parse →
    dedupe → plausibility review → honest counts.

    Downgrade semantics (spec verbatim — row KEPT, never deleted): a ping
    whose implied-or-reported speed exceeds IMPOSSIBLE_SPEED_KMH is WRITTEN
    with confidence=ESTIMATE and its speed column filled (the device's
    reported speed, or the implied speed when the device gave none — so the
    row can explain its own label later). Everything else keeps LIVE_GPS.
    """
    now = _now(now)

    # STEP 0 — claims arrive as CLAIMS: any non-int garbage (a session
    # object echo, a string id, a dict) is a WRONG CREDENTIAL, not a
    # TypeError. Uniform refusal instead of a 500 keeps the endpoint from
    # becoming a shape-check oracle.
    session_id = getattr(session_id, "pk", session_id)
    vehicle_id = getattr(vehicle_id, "pk", vehicle_id)
    if isinstance(session_id, str) and session_id.isdigit():
        session_id = int(session_id)
    if isinstance(vehicle_id, str) and vehicle_id.isdigit():
        vehicle_id = int(vehicle_id)
    if not isinstance(session_id, int) or isinstance(session_id, bool):
        raise SessionAuthRefused(MSG_AUTH_REFUSED)
    if not isinstance(vehicle_id, int) or isinstance(vehicle_id, bool):
        raise SessionAuthRefused(MSG_AUTH_REFUSED)

    # STEP 1 — 27's token door (None for ANY wrong credential):
    session = services.verify_session_token(
        raw_token, session_id=session_id, vehicle_id=vehicle_id
    )
    if session is None:
        raise SessionAuthRefused(MSG_AUTH_REFUSED)

    # STEP 2 — the deadline rule: tokens authenticate, LIVEity authorizes.
    if services.session_status(session, now=now) != SessionStatus.LIVE.value:
        raise ValidationError(MSG_PUBLISHING_OVER)

    # STEP 3 — the batch contract (≤50, spec verbatim): honest refusal.
    if isinstance(pings, (str, bytes)) or not isinstance(pings, (list, tuple)):
        raise ValidationError(MSG_BAD_BATCH)
    if len(pings) > INGEST_MAX_BATCH:
        raise ValidationError(MSG_BATCH_TOO_LARGE % {"limit": INGEST_MAX_BATCH})

    parsed = _parse_items(pings)

    # STEP 4 — dedupe on (session, recorded_at): FIRST occurrence wins.
    duplicates = 0
    in_batch: dict[datetime, dict] = {}
    for row in parsed:
        if row["recorded_at"] in in_batch:
            duplicates += 1  # repeated inside the same flush
        else:
            in_batch[row["recorded_at"]] = row
    # ...and against rows already stored (27's unique is the floor): an
    # offline queue replayed after a reconnect lands here, not as a 500.
    known = set(
        PingPoint.objects.filter(
            session=session, recorded_at__in=list(in_batch)
        ).values_list("recorded_at", flat=True)
    )
    duplicates += sum(1 for moment in in_batch if moment in known)
    candidates = [row for moment, row in in_batch.items() if moment not in known]

    # STEP 5 — plausibility review + write. Chain anchor: the last STORED
    # ping before this flush (the previous flush's tail), then each accepted
    # in-batch ping chains the next. Devices flush chronologically (the
    # offline queue replays oldest-first), so this walk mirrors the fixes'
    # real order; a late/backwards step skips the check (negative or
    # crossed-time math could only manufacture a false verdict).
    prev_row = (
        PingPoint.objects.filter(session=session)
        .order_by("-recorded_at", "-pk")
        .first()
    )
    prev = (
        {"moment": prev_row.recorded_at,
         "lat": float(prev_row.lat), "lon": float(prev_row.lon)}
        if prev_row is not None
        else None
    )

    accepted = downgraded = 0
    for row in sorted(candidates, key=lambda c: c["recorded_at"]):
        moment, lat, lon, reported = (
            row["recorded_at"], row["lat"], row["lon"], row["speed"]
        )
        point = (float(lat), float(lon))
        implied = implied_speed_kmh(
            prev["moment"] if prev is not None else None,
            (prev["lat"], prev["lon"]) if prev is not None else None,
            moment,
            point,
        )
        over_reported = (
            reported is not None and float(reported) > IMPOSSIBLE_SPEED_KMH
        )
        over_implied = implied is not None and implied > IMPOSSIBLE_SPEED_KMH
        if over_reported:
            speed = reported              # the device's own (impossible) claim
            confidence = Confidence.ESTIMATE.value
        elif over_implied:
            # implied is not None here by construction — the impossible move
            # goes IN the row (speed explains the ESTIMATE label later).
            speed = Decimal(str(round(implied, 2)))
            confidence = Confidence.ESTIMATE.value
        else:
            speed = reported
            confidence = Confidence.LIVE_GPS.value
        try:
            with transaction.atomic():  # savepoint: race backstop below
                PingPoint.objects.create(
                    session=session,
                    lat=lat,
                    lon=lon,
                    recorded_at=moment,
                    received_at=now,  # flush arrival — NOT the device stamp
                    speed_kmh=speed,
                    confidence=confidence,
                )
        except IntegrityError:
            # A concurrent flush wrote this (session, recorded_at) between
            # the pre-check and the insert: count it, don't fail the batch.
            duplicates += 1
        else:
            if confidence == Confidence.ESTIMATE.value:
                downgraded += 1
            else:
                accepted += 1
        prev = {"moment": moment, "lat": float(lat), "lon": float(lon)}

    return {
        "accepted": accepted,
        "downgraded": downgraded,
        "duplicates": duplicates,
        "batch_limit": INGEST_MAX_BATCH,
        "session_id": session.pk,
        "vehicle_id": session.vehicle_id,
    }


# ------------------------------------------------------------------- the view
def _error_body(error: str) -> dict:
    """The refused-ingest body — ALWAYS all-zero counts plus the reason."""
    return {"accepted": 0, "downgraded": 0, "duplicates": 0, "error": error}


@csrf_exempt          # ← WHY: the crew device holds no session cookie; CSRF
                      # protects cookie-authenticated state changes, while
                      # THIS endpoint authenticates by X-Tracking-Token.
def ping_batch(request, session_id: int):
    """POST tracking:ingest — the one door Prompt 29's flusher calls.

    Typed refusals: SessionAuthRefused → 403 with the ONE uniform message
    (no existence info, never 403-with-detail); every other ValidationError
    → 400 with its plain, actionable message; GET → 405 JSON (devices get
    JSON, never an HTML error page).
    """
    if request.method != "POST":
        return JsonResponse(
            _error_body(
                _("Use POST with the X-Tracking-Token header to upload a "
                  "ping batch.")
            ),
            status=405,
        )
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse(
            _error_body(_("Pings too large — the batch is smaller than that.")),
            status=400,
        )

    # The credential comes off the header FIRST.
    raw_token = request.headers.get(INGEST_HEADER, "")

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            _error_body(
                "Body is not valid JSON — send {\"vehicle_id\": <int>, "
                "\"pings\": [<positions>]}."
            ),
            status=400,
        )
    if not isinstance(body, dict):
        return JsonResponse(_error_body(str(MSG_BAD_BATCH)), status=400)

    try:
        result = ingest_pings(
            raw_token,
            session_id,
            body.get("vehicle_id"),
            body.get("pings", []),
        )
    except SessionAuthRefused:
        # Uniform 403 — the caller learns nothing about sessions it
        # cannot use (the message is identical for every bad credential).
        return JsonResponse(_error_body(str(MSG_AUTH_REFUSED)), status=403)
    except ValidationError as exc:
        return JsonResponse(_error_body("; ".join(exc.messages)), status=400)
    return JsonResponse(result)
