"""services.py — THE query layer that owns every privacy rule (Prompt 27).

Prompt 27's pitfall is honored structurally: suppression and visibility are
FUNCTIONS HERE, not template filters. Prompt 28's map, the views below and
any future API consumer must all come through these doors — there is no
other path to a position.

PUBLIC API (for Prompts 28 + 29, sweeps and tests):

    start_session(vehicle, *, by, cap_hours=None, now=None)
        -> (session, raw_token)        # raw token is DISPLAY-ONCE
    end_session(session, *, by, now=None) -> session
    rotate_token(session, *, by, now=None) -> raw_token   # old token DEAD
    visible_positions(session, *, now) -> QuerySet[PingPoint]
        # THE privacy door: live-only gate + 72 h cut + boundary suppression
    session_status(session, *, now) -> "LIVE" | "ENDED" | "EXPIRED"
    live_session_for(vehicle, *, now) -> TrackingSession | None
    authorizable_vehicles(user) -> QuerySet[Vehicle]
    authorization_for(user, vehicle) -> AuthorizationSource | None
    can_authorize(user, vehicle) -> bool
    verify_session_token(raw_token, *, session_id, vehicle_id)
        -> TrackingSession | None                      # 28's auth door
    expire_sessions(*, now=None) -> int                # cron sweep
    prune_pings(*, now=None) -> int                    # cron sweep

DPIA NOTE (KDPA 2019 — this module IS the mitigation; brief §5):
continuous precise location is high-risk processing. Rules IN CODE:
    (1) positions only while the session is LIVE at `now` (derived from
        ends_at None + started_at <= now < cap deadline — an expired-but-
        unswept session is ALREADY invisible, because the read derives from
        the deadline rather than from whether a cron ran);
    (2) auto-expiry: the sweep stamps ends_at at the cap; start_session
        stamps that vehicle's overdue rows first, so a forgotten stop can
        never widen the cap or block a fresh start;
    (3) ping retention 72 h (deletion sweep + read-time cut, independent
        of each other);
    (4) boundary suppression: the first and last effective pings of a
        session are ALWAYS suppressed, and everything inside the 1 km
        privacy radius around either anchor ping goes with them — off-trip
        homing/leaving patterns near the depot and the final stop are not
        reconstructable from anything this app ever exposes;
    (5) tokens: hashed at rest (sha256, scope = session id + vehicle id),
        rotation replaces (old immediately dead), raw shown exactly once,
        no crew identity in any public serialization.

AUTHORIZATION DOORS (fleet/routes PUBLIC services ONLY — no internal
tables, cross-app read hard rule): crew authority via
apps.routes.crew.is_approved_crew / vehicles_of_crew; operator authority
via apps.fleet.services.is_sacco_admin / visible_vehicles. Imports are
lazy inside function bodies (same pattern as routes.crew.py) so the app
registry can finish loading before these resolve. Platform staff get NO
bypass here (fleet's contract), and crew revocation closes the door the
same second fleet flips the seat.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    DEFAULT_CAP_HOURS,
    PING_RETENTION_HOURS,
    PRIVACY_RADIUS_METERS,
    AuthorizationSource,
    PingPoint,
    SessionEndedReason,
    SessionStatus,
    TrackingSession,
)

# ----------------------------------------------------------------- messages
MSG_LOGIN_REQUIRED = _("Log in to manage tracking sessions.")
MSG_NOT_AUTHORIZED = _(
    "Only approved crew of this vehicle, or a SACCO admin of its SACCO, "
    "may manage its tracking session."
)
MSG_VEHICLE_INACTIVE = _("Retired vehicles cannot publish live positions.")
MSG_ALREADY_LIVE = _(
    "This vehicle already has a live tracking session — one at a time."
)
MSG_BAD_CAP = _("Session cap must be between 1 and %(max)s hours.")

# Refusals for late "stop" presses: ending past the deadline would EXTEND
# the permission (ends_at later than the cap); honesty requires refusal —
# the cap already closed the session and its data is already invisible.
MSG_ALREADY_ENDED = _("That tracking session has already ended.")

TOKEN_PREFIX = "mtk_"  # recognisable on the wire, meaningless alone
# The digest BAKES the scope in: a captured token is worthless outside its
# own (session, vehicle) pair even before a lookup rejects it.
TOKEN_SCOPE_PREFIX = "mtaani-tracking/session"


def _now(now):
    """Injected-clock default (barter/listings sweep convention)."""
    return timezone.now() if now is None else now


# ------------------------------------------------------------------- tokens
def new_raw_token() -> str:
    """A fresh raw token: display-once, never stored (module docstring)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(24)


def token_digest(raw_token: str, *, session_id: int, vehicle_id: int) -> str:
    """sha256 of the raw token BOUND to (session id, vehicle id).

    WHY scope-baked: "token X unlocks session 1" implies nothing about
    session 2 even in a buggy lookup — the digest itself changes with the
    claimed scope, so a captured crew token cannot be replayed against
    another vehicle's pipeline.
    """
    scope = f"{TOKEN_SCOPE_PREFIX}/{session_id}/{vehicle_id}"
    return hashlib.sha256(f"{scope}|{raw_token}".encode("utf-8")).hexdigest()


def verify_session_token(
    raw_token: str, *, session_id: int, vehicle_id: int
) -> TrackingSession | None:
    """Prompt 28's auth door: recompute the SCOPE-BAKED digest for the
    claimed (session_id, vehicle_id) and match it at rest.

    Returns the session row or None — never raises, never leaks. None is
    the honest answer for any wrong token, wrong scope or wrong-vehicle
    claim (a stolen token replayed against another pipeline included).
    Callers then check session_status(session, now) before publishing —
    tokens authenticate; the deadline rule authorizes.
    """
    if not raw_token or session_id is None or vehicle_id is None:
        return None
    digest = token_digest(raw_token, session_id=session_id, vehicle_id=vehicle_id)
    return TrackingSession.objects.filter(
        pk=session_id, vehicle_id=vehicle_id, token_hash=digest
    ).first()


# ------------------------------------------------------------ authorization
def authorization_for(user, vehicle) -> AuthorizationSource | None:
    """The authorization door the user may use for THIS vehicle; None = refused.

    TWO doors only (brief): an APPROVED crew member starting their own
    vehicle's session (CREW — "self"), or a SACCO admin starting it for
    their SACCO's vehicle (SACCO_ADMIN). Deterministic when both hold: the
    OPERATOR door wins — the row records the stronger authorization fact.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if vehicle is None or not vehicle.pk:
        return None
    from apps.fleet import services as fleet_services  # lazy: registry order
    from apps.routes import crew as routes_crew        # lazy: registry order

    # Operator door first — fleet's PUBLIC tenant-wall door (is_sacco_admin
    # answers from SaccoAdmin data; no re-filtering, no internal tables).
    if fleet_services.is_sacco_admin(user, vehicle.sacco):
        return AuthorizationSource.SACCO_ADMIN
    # Crew door: APPROVED seat and THIS vehicle among the crew's own —
    # vehicles_of_crew already filters ACTIVE + APPROVED, so retired metal
    # and pending/revoked seats can never come back in through here.
    if routes_crew.vehicles_of_crew(user).filter(pk=vehicle.pk).exists():
        return AuthorizationSource.CREW
    return None


def can_authorize(user, vehicle) -> bool:
    """True ⇔ the user may start/end/rotate THIS vehicle's sessions."""
    return authorization_for(user, vehicle) is not None


def authorizable_vehicles(user) -> QuerySet:
    """Vehicles whose sessions `user` may manage: the crew's own set plus
    the operator's visible set.

    WHY a union of DOORS, not a re-filter: tenancy lives inside
    vehicles_of_crew (approved seats only) and visible_vehicles (the tenant
    wall) — this function only unions them, so a forged URL can still never
    reach another SACCO's vehicle through any view below. Anonymous and
    ordinary members get an empty queryset; platform staff get no bypass
    (fleet's contract).
    """
    from apps.fleet import services as fleet_services  # lazy: registry order
    from apps.routes import crew as routes_crew        # lazy: registry order

    return (
        routes_crew.vehicles_of_crew(user).distinct()
        | fleet_services.visible_vehicles(user).distinct()
    ).distinct().order_by("plate")


# ------------------------------------------------------------- session reads
def session_status(session, *, now) -> str:
    """DERIVED status at `now` — the read contract (module docstring:
    no stored status column, because drift on this model = leaking).

    LIVE    ends_at None and started_at <= now < deadline (publishing)
    EXPIRED deadline reached — swept (reason stamped) or not (unswept);
            both equally invisible to consumers
    ENDED   swept human stop (ends_at + reason STOPPED stamped)
    """
    if session.ends_at is not None:
        if session.ended_reason == SessionEndedReason.EXPIRED.value:
            return SessionStatus.EXPIRED.value
        return SessionStatus.ENDED.value
    if now < session.started_at or now >= session.deadline():
        return SessionStatus.EXPIRED.value
    return SessionStatus.LIVE.value


def live_session_for(vehicle, *, now) -> TrackingSession | None:
    """The vehicle's LIVE session at `now` (None when none) — the READ side
    of one-live-per-vehicle; the WRITE side is refused in start_session.

    Unswept overdue rows are DEAD here: the deadline check makes them
    unreturnable even before the cron stamps their ends_at.
    """
    if vehicle is None or not vehicle.pk:
        return None
    for row in vehicle.tracking_sessions.filter(ends_at__isnull=True):
        if session_status(row, now=now) == SessionStatus.LIVE.value:
            return row
    return None


def visible_positions(session, *, now) -> QuerySet:
    """THE one position door — every rule is enforced HERE (the pitfall).

    Returns a QuerySet[PingPoint] ordered recorded_at ASC; after the
    session ends or expires the result is EMPTY, provably, whatever the
    ping table holds.

    Rules, in enforced order (each also tested in
    tests/test_tracking_sessions.py):

      RULE 1 — live-only gate: unless session_status(session, now) is LIVE
      the door returns NOTHING. Acceptance wording: a session is visible
      while authorized and UTTERLY INVISIBLE after.

      RULE 2 — 72 h retention cut: pings recorded at or before
      `now - PING_RETENTION_HOURS` are excluded. The cut is the read-time
      twin of prune_pings (same inequality, same boundary) so the door can
      never expose a row the job is about to (or already did) delete.

      RULE 3 — boundary suppression (the off-trip guard): the session's
      first (start anchor) and last (end anchor) EFFECTIVE pings — the
      first/last pings WITHIN the retained window of RULE 2, so pruning
      never leaves a stale anchor over-suppressing — are ALWAYS suppressed
      (position-independent: they sit AT a boundary by definition — a ping
      at the boundary IS the boundary), and every ping inside the 1 km
      PRIVACY radius of either anchor ping is suppressed too (ST_DWithin
      inclusive <= semantics on geography, the ADR-0001 SQL-cast route). A
      single-ping session: that one ping is both first and last-anchor →
      suppressed → EMPTY.
    """
    now = _now(now)
    if session_status(session, now=now) != SessionStatus.LIVE.value:
        return PingPoint.objects.none()  # RULE 1 — provably empty after

    cutoff = now - timedelta(hours=PING_RETENTION_HOURS)
    windowed = PingPoint.objects.filter(
        session=session, recorded_at__gt=cutoff  # RULE 2
    )

    first = windowed.order_by("recorded_at", "pk").first()   # start anchor
    last = windowed.order_by("-recorded_at", "-pk").first()  # end anchor
    if first is None or last is None or last.pk == first.pk:
        # RULE 3 edge: empty window, or the one ping is BOTH anchors —
        # suppressed entirely (its position is its own boundary).
        return PingPoint.objects.none()

    suppressed = _boundary_suppressed_pks(session, first, last, cutoff)
    return windowed.exclude(pk__in=suppressed).order_by("recorded_at", "pk")


def _boundary_suppressed_pks(session, first, last, cutoff) -> set:
    """The pks RULE 3 kills: the two anchors themselves + the <= 1 km rings
    around them (PostGIS geodesic via SQL cast — ADR-0001; ST_DWithin is
    INCLUSIVE of the boundary, same settled semantics as Prompt 19).

    WHY raw SQL: the distance predicate is real PostGIS geodesic math (the
    same math the map's neighbourhood reads use) and keeping the ring in
    ONE statement keeps the suppression decision in ONE place — this
    module, never a template. ST_MakePoint is (x=lon, y=lat) — PostGIS
    order, the one lon/lat translation point per apps.core.geo's rule.
    """
    ring_sql = f"""
SELECT p.id
FROM tracking_pingpoint p
WHERE p.session_id = %(session_id)s
  AND p.recorded_at > %(cutoff)s
  AND (
        p.id IN (%(anchor_a_pk)s, %(anchor_b_pk)s)
        OR ST_DWithin(
             ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography,
             ST_SetSRID(ST_MakePoint(%(a_lon)s, %(a_lat)s), 4326)::geography,
             %(radius_m)s)
        OR ST_DWithin(
             ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326)::geography,
             ST_SetSRID(ST_MakePoint(%(b_lon)s, %(b_lat)s), 4326)::geography,
             %(radius_m)s)
      )
"""
    params = {
        "session_id": session.pk,
        "cutoff": cutoff,
        "anchor_a_pk": first.pk,
        "anchor_b_pk": last.pk,
        # NOTE (ADR-0001): Decimal params bind into ST_MakePoint fine.
        "a_lat": first.lat, "a_lon": first.lon,
        "b_lat": last.lat, "b_lon": last.lon,
        "radius_m": PRIVACY_RADIUS_METERS,
    }
    with connection.cursor() as cursor:
        cursor.execute(ring_sql, params)
        return {row[0] for row in cursor.fetchall()}


# -------------------------------------------------------------- session writes
@transaction.atomic
def start_session(vehicle, *, by, cap_hours=None, now=None) -> tuple:
    """Open an authorized session → (session, raw_token_for_display_once).

    Gates, in order (every refusal tested):
      * by must be a real member (anonymous → refused);
      * the vehicle must be ACTIVE (retired metal = authority un-granted);
      * an authorization door must open: APPROVED crew of THAT vehicle
        (self) or SACCO admin of the vehicle's SACCO — everything else is
        refused, every path;
      * the cap: default DEFAULT_CAP_HOURS; a per-session cap may be
        SHORTER, never longer (the brief's cap is a CEILING);
      * one LIVE session per vehicle (derived-live, including unswept
        overdue rows which are stamped EXPIRED first so the DB partial
        index and reality agree).
    The raw token is returned exactly once; only its scope-baked sha256 is
    stored (display-once contract, enforced by the views' one-time page).
    """
    now = _now(now)
    if by is None or not getattr(by, "is_authenticated", False):
        raise ValidationError(MSG_LOGIN_REQUIRED)
    if vehicle is None or not vehicle.pk:
        raise ValidationError(MSG_NOT_AUTHORIZED)
    # Enum VOCABULARY (not a table read) from fleet — lazy, registry-order-
    # safe, same reason routes.crew.py imports lazily.
    from apps.fleet.models import VehicleStatus

    if vehicle.status != VehicleStatus.ACTIVE.value:
        raise ValidationError(MSG_VEHICLE_INACTIVE)
    source = authorization_for(by, vehicle)
    if source is None:
        raise ValidationError(MSG_NOT_AUTHORIZED)

    seconds_cap = DEFAULT_CAP_HOURS if cap_hours is None else cap_hours
    # bool is an int subclass — forbid bools explicitly, they are a bug.
    if isinstance(seconds_cap, bool) or not isinstance(seconds_cap, int):
        raise ValidationError(MSG_BAD_CAP % {"max": DEFAULT_CAP_HOURS})
    if not (1 <= seconds_cap <= DEFAULT_CAP_HOURS):
        raise ValidationError(MSG_BAD_CAP % {"max": DEFAULT_CAP_HOURS})

    _stamp_overdue(vehicle, now)  # overdue-and-unswept → EXPIRED (see below)

    if live_session_for(vehicle, now=now) is not None:
        raise ValidationError(MSG_ALREADY_LIVE)

    session = TrackingSession(
        vehicle=vehicle,
        authorized_by=by,
        authorization_source=source.value,
        started_at=now,
        cap_hours=seconds_cap,
    )
    session.full_clean()  # model floor: cap bounds, source vocabulary
    session.save()        # needs the pk to bake the token's scope
    raw_token = new_raw_token()  # exists ONLY on the return path
    session.token_hash = token_digest(
        raw_token, session_id=session.pk, vehicle_id=vehicle.pk
    )
    session.save(update_fields=["token_hash", "updated_at"])
    return session, raw_token


def _stamp_overdue(vehicle, now) -> int:
    """Stamp THIS vehicle's unswept overdue rows EXPIRED at their cap.

    WHY inside start_session: cap deadlines are enforced by the DERIVED
    read, but the DB's one-live partial index keys on ends_at NULL — a
    forgotten-but-overdue row blocks nothing logically, yet would trip the
    index. Stamping first keeps both true at once. The stamped ends_at is
    the row's CAP DEADLINE (when it truly stopped publishing), never the
    sweep moment — a record must not claim it stayed live longer than its
    authorization. WHY row-by-row: each row's deadline differs; the set is
    tiny (per vehicle). Returns rows stamped.
    """
    stamped = 0
    for row in TrackingSession.objects.filter(vehicle=vehicle, ends_at__isnull=True):
        deadline = row.deadline()
        if deadline <= now:
            TrackingSession.objects.filter(pk=row.pk).update(
                ends_at=deadline,  # the honest end moment: its own cap
                ended_reason=SessionEndedReason.EXPIRED.value,
                updated_at=now,
            )
            stamped += 1
    return stamped


@transaction.atomic
def end_session(session, *, by, now=None) -> TrackingSession:
    """Crew/admin STOP: ends_at=`now`, reason STOPPED (the honest door)."""
    now = _now(now)
    if by is None or not getattr(by, "is_authenticated", False):
        raise ValidationError(MSG_LOGIN_REQUIRED)
    if session.pk is None:
        raise ValidationError(MSG_NOT_AUTHORIZED)
    if authorization_for(by, session.vehicle) is None:
        raise ValidationError(MSG_NOT_AUTHORIZED)
    # Refuse anything not live AT `now`: re-ending a swept row is a no-op
    # lie, and ending an unswept-expired row would PUSH ends_at past the
    # cap — a permission extension through the stop button. The cap
    # already closed those sessions; their data is already invisible.
    if session_status(session, now=now) != SessionStatus.LIVE.value:
        raise ValidationError(MSG_ALREADY_ENDED)
    session.ends_at = now
    session.ended_reason = SessionEndedReason.STOPPED.value
    session.full_clean()
    session.save(update_fields=["ends_at", "ended_reason", "updated_at"])
    return session


@transaction.atomic
def rotate_token(session, *, by, now=None) -> str:
    """Rotation: a NEW raw token; the old one is DEAD on the spot.

    WHY only while live: a rotation on an ended/expired session mints a
    credential for an authorization that no longer exists. The stored
    digest is REPLACED, so the old raw fails verify_session_token
    immediately — no grace period for a leaked credential.
    """
    now = _now(now)
    if by is None or not getattr(by, "is_authenticated", False):
        raise ValidationError(MSG_LOGIN_REQUIRED)
    if session.pk is None:
        raise ValidationError(MSG_NOT_AUTHORIZED)
    if authorization_for(by, session.vehicle) is None:
        raise ValidationError(MSG_NOT_AUTHORIZED)
    if session_status(session, now=now) != SessionStatus.LIVE.value:
        raise ValidationError(MSG_ALREADY_ENDED)
    raw_token = new_raw_token()
    session.token_hash = token_digest(
        raw_token, session_id=session.pk, vehicle_id=session.vehicle_id
    )
    session.save(update_fields=["token_hash", "updated_at"])
    return raw_token


# ------------------------------------------------------------------- sweeps
def expire_sessions(*, now=None) -> int:
    """The auto-expiry sweep (cron/admin hook; barter's sweep pattern).

    Rows PUBLISHING by derived deadline whose cap moment is at or before
    `now` are stamped ended_reason=EXPIRED with ends_at = the row's OWN
    cap deadline (the honest end moment — a sweep that ran hours late must
    not rewrite history to say the session outlived its authorization).
    Boundary contract (mirrors the siblings): at EXACTLY the cap the row
    expires (deadline <= now); injected clock in, rows moved out.

    WHY a loop + bulk updates (like barter's sweep): the derived deadline
    is per-row arithmetic; correctness beats a cleverer one-SQL form, and
    the unswept-live set is bounded by active vehicles.
    Returns the NUMBER OF ROWS expired.
    """
    now = _now(now)
    overdue = [
        row for row in TrackingSession.objects.filter(ends_at__isnull=True)
        if row.deadline() <= now
    ]
    if not overdue:
        return 0
    # One bulk move per row (few rows; per-row honest end moments):
    for row in overdue:
        TrackingSession.objects.filter(pk=row.pk).update(
            ends_at=row.deadline(),
            ended_reason=SessionEndedReason.EXPIRED.value,
            updated_at=now,
        )
    return len(overdue)


def prune_pings(*, now=None) -> int:
    """The 72 h retention job (cron/admin hook; sibling sweep pattern).

    Deletes ping rows captured at or before `now - PING_RETENTION_HOURS`.
    Boundary contract: at EXACTLY 72 h the row goes (recorded_at <=
    now - window) — identical semantics to the visibility cut in
    visible_positions, so no consumer ever sees a row the job deletes.
    Sessions are NOT deleted: an ended session outliving its pings renders
    an honest "nothing recorded" state, never invented history.

    DPIA note: this is data minimization in code — positions are retained
    only as long as the map needs, then gone. Returns the number of ping
    rows deleted.
    """
    now = _now(now)
    cutoff = now - timedelta(hours=PING_RETENTION_HOURS)
    _, per_model = PingPoint.objects.filter(recorded_at__lte=cutoff).delete()
    # delete() returns counts per model; count THIS model's share exactly.
    return per_model.get(PingPoint._meta.label, 0)
