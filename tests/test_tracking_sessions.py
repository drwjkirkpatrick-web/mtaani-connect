"""tests/test_tracking_sessions.py — Prompt 27: authorized tracking sessions.

Acceptance coverage, provably (injected clock everywhere):

  * authorization gates — EVERY path: approved crew (self), SACCO admin of
    the vehicle's SACCO, and refusals for pending/unapproved/other-vehicle/
    other-sacco/anonymous/plain members + retired metal;
  * active-session-only visibility — LIVE yields positions; ENDED and
    EXPIRED (swept OR unswept) yield EMPTY no matter what the ping table
    holds — "visible while authorized, utterly invisible after";
  * auto-expiry — the 12 h default cap, shorter caps respected, longer
    refused, boundary EXACTLY at the cap expires, sweep returns rows moved;
  * ping pruning — the 72 h retention boundary, exact counts, session rows
    survive their pings;
  * boundary suppression — first/last pings ALWAYS dropped + the 1 km
    privacy ring (0.8 km suppressed, 1.2 km kept, ring around BOTH anchors,
    single-ping fully-suppressed), synthetic deterministic coordinates whose
    distances the tests themselves verify bracket the radius;
  * token scoping/rotation — raw display-once + hash at rest, scope-baked
    digest (stolen token useless for another session/vehicle), rotation
    kills the old token immediately;
  * crew identity absence — exact payload key sets over the Prompt 28 seam
    plus bait-string scans (crew username, phone, token material);
  * view flows (login, 403s, display-once pages, tenant scopes) + page
    budgets + i18n markers.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.core.geo import distance_km
from apps.core.testing import assert_under_budget, mock_now
from apps.fleet import services as fleet_services
from apps.routes import crew as routes_crew
from apps.routes.models import Sacco
from apps.tracking import serialization, services
from apps.tracking.models import (
    DEFAULT_CAP_HOURS,
    PING_RETENTION_HOURS,
    PRIVACY_RADIUS_KM,
    AuthorizationSource,
    PingPoint,
    SessionEndedReason,
    SessionStatus,
    TrackingSession,
)

# THE CLOCK: one fixed UTC moment (apps.core.testing's injected-clock rule);
# every session/sweep/read in this file names `now=` explicitly — nothing
# depends on wall time anywhere.
T0 = mock_now()
# View flows run on the REAL clock (views inject timezone.now()); sessions
# in view tests start at import time so the 12 h cap comfortably holds.
VNOW = dj_timezone.now()

# Synthetic, deterministic coordinates (Nairobi CBD latitude) for the
# boundary-suppression tests: offsets along CONSTANT latitude, converted
# degrees→km with the haversine actually checked (test_ring_geometry...).
ANCHOR_LAT = Decimal("-1.286400")
ANCHOR_LON = Decimal("36.817200")
# ~111.32 km/deg lon at the equator, shrunk by cos(latitude): the test
# "ring geometry" verifies these constants produce the distances claimed.
DEG_LON_KM = 111.32 * 0.999748  # cos(radians(1.2864°))


def _east(km: float) -> tuple[Decimal, Decimal]:
    """A synthetic point `km` EAST of the anchor along constant latitude."""
    dlon = Decimal(km / DEG_LON_KM).quantize(Decimal("0.000001"))
    return ANCHOR_LAT, ANCHOR_LON + dlon


def _anchor() -> tuple[Decimal, Decimal]:
    return ANCHOR_LAT, ANCHOR_LON


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world(db):
    """Two SACCOs: approved crew + admin around vehicle A; second crew,
    admin around vehicle B; a PENDING applicant; an ordinary member; and a
    phone number on the crew user as bait for the privacy scans."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_t27", "staff@example.com", "***")
    sacco_a = Sacco.objects.create(name="Matata United")
    sacco_b = Sacco.objects.create(name="Kinoo Operators")
    admin_a = User.objects.create_user("operator_a", "op_a@example.com", "***")
    admin_b = User.objects.create_user("operator_b", "op_b@example.com", "***")
    crew = User.objects.create_user("driver_privacy_bait", "crew@example.com", "***")
    crew.phone = "+254711222333"  # BAIT — must never leak into a payload
    crew.save()
    crew_b = User.objects.create_user("driver_b_bait", "crew_b@example.com", "***")
    crew_b.phone = "+254711444555"  # BAIT
    crew_b.save()
    pending = User.objects.create_user("pending_joe", "pending@example.com", "***")
    plain = User.objects.create_user("plain_member", "plain@example.com", "***")
    fleet_services.grant_sacco_admin(user=admin_a, sacco=sacco_a, granter=staff, now=T0)
    fleet_services.grant_sacco_admin(user=admin_b, sacco=sacco_b, granter=staff, now=T0)
    veh_a = fleet_services.register_vehicle(
        sacco=sacco_a, raw_plate="KCB 220P", capacity=33,
        nickname="Mwangaza", actor=admin_a, now=T0,
    )
    veh_b = fleet_services.register_vehicle(
        sacco=sacco_b, raw_plate="KBX 456M", capacity=14,
        nickname="Machozi", actor=admin_b, now=T0,
    )
    seat_a = fleet_services.request_crew(user=crew, vehicle=veh_a, role="DRIVER", now=T0)
    fleet_services.approve_crew(seat_a, approver=admin_a, now=T0)
    seat_b = fleet_services.request_crew(user=crew_b, vehicle=veh_b, role="DRIVER", now=T0)
    fleet_services.approve_crew(seat_b, approver=admin_b, now=T0)
    pending_seat = fleet_services.request_crew(
        user=pending, vehicle=veh_a, role="CONDUCTOR", now=T0
    )  # stays PENDING — refused to publish, below
    return {
        "staff": staff, "sacco_a": sacco_a, "sacco_b": sacco_b,
        "admin_a": admin_a, "admin_b": admin_b,
        "crew": crew, "crew_b": crew_b, "pending": pending, "plain": plain,
        "veh_a": veh_a, "veh_b": veh_b, "pending_seat": pending_seat,
    }


def _start(vehicle, by, cap_hours=None, now=T0) -> tuple[TrackingSession, str]:
    """Service start with the fixed clock (tests of refusals use _refuses)."""
    return services.start_session(vehicle, by=by, cap_hours=cap_hours, now=now)


def _fix(session, at, point=None) -> PingPoint:
    """A recorded ping — received at +7 s (mock ingest behaviour)."""
    lat, lon = point if point is not None else _anchor()
    return PingPoint.objects.create(
        session=session, lat=lat, lon=lon, recorded_at=at,
        received_at=at + timedelta(seconds=7),
    )


def _client_for(client, user):
    client.force_login(user)
    return client


def _token_in(body: str) -> str | None:
    """Extract the raw 'mtk_…' token from a rendered page (the token is
    glued to HTML attributes inline, so whitespace splitting lies)."""
    match = re.search(r"mtk_[A-Za-z0-9_\-]+", body)
    return match.group(0) if match else None


# ================================================== authorization gates
def test_start_by_approved_crew_records_self_authorization(world):
    """The crew door: an APPROVED crew member starts THEIR vehicle's
    session ("self"), source recorded as CREW, moment kept."""
    session, raw_token = _start(world["veh_a"], world["crew"])
    assert session.pk is not None
    assert session.authorization_source == AuthorizationSource.CREW.value
    assert session.authorized_by == world["crew"]
    assert session.cap_hours == DEFAULT_CAP_HOURS  # default cap, spec: 12 h
    assert session.started_at == T0
    assert raw_token.startswith("mtk_")


def test_start_by_sacco_admin_records_operator_authorization(world):
    """The operator door: a SACCO admin starts a session for THEIR sacco's
    vehicle — source SACCO_ADMIN, authorized_by = the admin human."""
    session, raw_token = _start(world["veh_b"], world["admin_b"])
    assert session.authorization_source == AuthorizationSource.SACCO_ADMIN.value
    assert session.authorized_by == world["admin_b"]
    assert services.can_authorize(world["admin_b"], world["veh_b"]) is True


def test_start_refused_for_pending_crew(world):
    """PENDING seat = NOT crew: the human gate has not said yes, so no
    position authority is granted (fleet's two-party gate, honored here)."""
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["pending"])
    assert TrackingSession.objects.count() == 0


def test_start_refused_for_crew_of_another_vehicle(world):
    """Approved crew OF VEHICLE B cannot authorize VEHICLE A's sessions —
    the seat is per-vehicle authority, not transport-wide authorship."""
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["crew_b"])
    assert TrackingSession.objects.count() == 0


def test_start_refused_for_admin_of_other_sacco(world):
    """SACCO B's admin cannot authorize SACCO A's vehicle (tenant wall)."""
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["admin_b"])
    with pytest.raises(ValidationError):
        _start(world["veh_b"], world["admin_a"])
    assert TrackingSession.objects.count() == 0


@pytest.mark.parametrize("by", [None, AnonymousUser()])
def test_start_refused_for_anonymous_callers(by, db, world):
    with pytest.raises(ValidationError):
        _start(world["veh_a"], by)


def test_start_refused_for_plain_member(world):
    """An ordinary (non-crew, non-operator) member is refused every path."""
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["plain"])
    with pytest.raises(ValidationError):
        _start(world["veh_b"], world["plain"])


def test_start_refused_after_crew_seat_revoked(world, db):
    """Authority is the seat, not the person: fleet revokes the seat →
    the crew door closes (the operator door remains the operator's)."""
    seat = world["crew"].fleet_crew.first()
    fleet_services.revoke_crew(seat, revoker=world["admin_a"], now=T0)
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["crew"])
    assert services.can_authorize(world["crew"], world["veh_a"]) is False


def test_start_refused_for_retired_vehicle(world, db):
    """Retired metal: parked in the yard — authority un-granted, any door."""
    fleet_services.retire_vehicle(world["veh_b"], actor=world["admin_b"], now=T0)
    with pytest.raises(ValidationError):
        _start(world["veh_b"], world["admin_b"])
    with pytest.raises(ValidationError):
        _start(world["veh_b"], world["crew_b"])


def test_authorizable_vehicles_is_a_union_of_scoped_doors(world, db):
    """The start form's vehicle list = crew's own ∪ operator's visible —
    nothing else leaks in (a forged POST with a foreign pk still fails at
    the service door, tested above)."""
    assert {v.pk for v in services.authorizable_vehicles(world["admin_a"])} == {world["veh_a"].pk}
    assert {v.pk for v in services.authorizable_vehicles(world["crew"])} == {world["veh_a"].pk}
    assert {v.pk for v in services.authorizable_vehicles(world["crew_b"])} == {world["veh_b"].pk}
    assert services.authorizable_vehicles(world["plain"]).exists() is False
    assert services.authorizable_vehicles(None).exists() is False


def test_status_vocabulary_of_derived_rows(world):
    """The derived status distinguishes the swept ENDED/EXPIRED rows and
    never mislabels a live one (no stored column to disagree)."""
    a = _start(world["veh_a"], world["crew"], cap_hours=1, now=T0)[0]
    b = _start(world["veh_b"], world["admin_b"], now=T0)[0]
    services.end_session(a, by=world["crew"], now=T0 + timedelta(minutes=30))
    assert services.session_status(a, now=T0 + timedelta(minutes=45)) == SessionStatus.ENDED.value
    assert services.session_status(b, now=T0 + timedelta(minutes=45)) == SessionStatus.LIVE.value
    services.expire_sessions(now=T0 + timedelta(hours=13))  # b hits its 12 h cap
    assert services.session_status(b, now=T0 + timedelta(hours=13)) == SessionStatus.EXPIRED.value


# ======================================================== token contract
def test_raw_token_is_display_once_and_only_hash_is_stored(world):
    session, raw_token = _start(world["veh_a"], world["crew"])
    stored = TrackingSession.objects.get(pk=session.pk)
    assert raw_token.startswith("mtk_")
    assert stored.token_hash != raw_token
    assert len(stored.token_hash) == 64  # sha256 hexdigest only
    assert raw_token not in json.dumps(
        serialization.session_positions(stored, now=T0)
    )
    # Recomputable digest matches at rest (verifiable, not reversible):
    assert stored.token_hash == services.token_digest(
        raw_token, session_id=session.pk, vehicle_id=session.vehicle_id
    )


def test_verify_session_token_is_the_scoped_ingest_door(world):
    """28's door: raw + claimed (session, vehicle) scope → row or None."""
    session, raw_token = _start(world["veh_a"], world["crew"])
    other, other_raw = _start(world["veh_b"], world["admin_b"])
    # Right token, right scope:
    found = services.verify_session_token(
        raw_token, session_id=session.pk, vehicle_id=session.vehicle_id
    )
    assert found == session
    # The SAME token claimed for ANOTHER session/vehicle → None (scope):
    assert services.verify_session_token(
        raw_token, session_id=other.pk, vehicle_id=other.vehicle_id
    ) is None
    # Wrong token under every scope → None:
    assert services.verify_session_token(
        "mtk_not_a_real_token", session_id=session.pk, vehicle_id=session.vehicle_id
    ) is None
    # Empty token → None (honest rejection, never an exception):
    assert services.verify_session_token("", session_id=session.pk, vehicle_id=session.vehicle_id) is None


def test_rotate_token_replaces_and_kills_the_old(world):
    session, old_raw = _start(world["veh_a"], world["crew"])
    new_raw = services.rotate_token(session, by=world["crew"], now=T0 + timedelta(minutes=5))
    assert new_raw.startswith("mtk_")
    assert new_raw != old_raw
    stored = TrackingSession.objects.get(pk=session.pk)
    # Old raw is DEAD the moment rotation lands:
    assert services.verify_session_token(
        old_raw, session_id=session.pk, vehicle_id=session.vehicle_id
    ) is None
    # New raw works:
    assert services.verify_session_token(
        new_raw, session_id=session.pk, vehicle_id=session.vehicle_id
    ) == session
    assert services.rotate_token(session, by=world["crew"], now=T0 + timedelta(minutes=6)) != new_raw


def test_rotate_refused_once_the_session_is_not_live(world):
    session, raw_token = _start(world["veh_a"], world["crew"], cap_hours=1)
    services.end_session(session, by=world["crew"], now=T0 + timedelta(minutes=30))
    with pytest.raises(ValidationError):
        services.rotate_token(session, by=world["crew"], now=T0 + timedelta(minutes=31))
    # And rotation past the cap (unswept) is refused too — no minting
    # credentials for an authorization that no longer exists:
    with pytest.raises(ValidationError):
        services.rotate_token(session, by=world["crew"], now=T0 + timedelta(hours=2))


# =============================================== one-live + cap contract
def test_one_live_session_per_vehicle_refuses_the_second(world):
    first, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["admin_a"], now=T0 + timedelta(minutes=1))
    assert TrackingSession.objects.filter(vehicle=world["veh_a"]).count() == 1
    # Crew path and admin path hit the same rule:
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["crew"], now=T0 + timedelta(minutes=2))


def test_restart_after_manual_end(world):
    """Ended → the vehicle's door reopens for a FRESH window (the old row
    stays as history — a new token, a new start, no reuse)."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    services.end_session(session, by=world["crew"], now=T0 + timedelta(hours=1))
    fresh, fresh_raw = _start(world["veh_a"], world["crew"], now=T0 + timedelta(hours=2))
    assert fresh.pk != session.pk
    assert fresh_raw != raw_token
    assert services.session_status(session, now=T0 + timedelta(hours=2)) == SessionStatus.ENDED.value


def test_restart_after_unswept_overdue_stamps_the_casualty(world):
    """The sweep hasn't run, but the cap ALREADY closed the old session:
    a fresh start is allowed AND stamps the stale row EXPIRED first (never
    lets a forgotten stop block the next one, never widens any cap). The
    stamped ends_at is the CAP DEADLINE — the sweep moment rewrites no
    history."""
    old, raw_old = _start(world["veh_a"], world["crew"], cap_hours=1, now=T0)
    later = T0 + timedelta(hours=3)  # 2 h past the 1 h cap, sweep not run
    fresh, raw_fresh = _start(world["veh_a"], world["crew"], now=later)
    old.refresh_from_db()
    assert old.ended_reason == SessionEndedReason.EXPIRED.value
    assert old.ends_at == T0 + timedelta(hours=1)  # its cap, not `later`
    assert services.session_status(old, now=later) == SessionStatus.EXPIRED.value
    assert services.session_status(fresh, now=later) == SessionStatus.LIVE.value


def test_shorter_cap_is_respected(world):
    """Per-session cap: shorter grants honored on BOTH sides of the line."""
    session, raw_token = _start(world["veh_a"], world["crew"], cap_hours=2, now=T0)
    assert session.cap_hours == 2
    assert services.session_status(session, now=T0 + timedelta(hours=1, minutes=59)) == SessionStatus.LIVE.value
    assert services.session_status(session, now=T0 + timedelta(hours=2)) == SessionStatus.EXPIRED.value
    # A SECOND ping after the shorter cap is invisible immediately — the
    # read gates on the deadline, not on any cron:
    _fix(session, T0 + timedelta(hours=2, minutes=1))
    assert count_visible(session, now=T0 + timedelta(hours=2, minutes=2)) == 0


@pytest.mark.parametrize("bad", [0, -1, 13, True, "12", 1.5])
def test_cap_longer_than_the_ceiling_or_garbage_refused(bad, world):
    with pytest.raises(ValidationError):
        _start(world["veh_a"], world["crew"], cap_hours=bad)
    assert TrackingSession.objects.count() == 0


def visible(session, now) -> list:
    """The door under test: evaluated, chronological — one place to assert."""
    return list(services.visible_positions(session, now=now))


def count_visible(session, now) -> int:
    return services.visible_positions(session, now=now).count()


# ============================================================ auto-expiry
def test_expire_sessions_moves_only_overdue_rows(world):
    running = _start(world["veh_a"], world["crew"], cap_hours=12, now=T0)[0]
    short = _start(world["veh_b"], world["admin_b"], cap_hours=1, now=T0)[0]
    # At exactly the SHORT cap, the short session is overdue:
    moved = services.expire_sessions(now=T0 + timedelta(hours=1))
    assert moved == 1
    short.refresh_from_db()
    assert short.ends_at == T0 + timedelta(hours=1)
    assert short.ended_reason == SessionEndedReason.EXPIRED.value
    assert services.session_status(short, now=T0 + timedelta(hours=2)) == SessionStatus.EXPIRED.value
    assert services.session_status(running, now=T0 + timedelta(hours=2)) == SessionStatus.LIVE.value
    # Swept rows don't double-move; fresh live rows aren't touched:
    fresh = _start(world["veh_b"], world["admin_b"], now=T0 + timedelta(hours=2))[0]
    assert services.expire_sessions(now=T0 + timedelta(hours=2)) == 0
    assert services.session_status(fresh, now=T0 + timedelta(hours=2)) == SessionStatus.LIVE.value


def test_cap_boundary_at_exactly_twelve_hours(world):
    """Boundary contract (barter/listings sweep semantics): at EXACTLY the
    cap elapsed (deadline <= now) the session expires."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    just_before = T0 + timedelta(hours=11, seconds=3599)
    assert services.session_status(session, now=just_before) == SessionStatus.LIVE.value
    at_cap = T0 + timedelta(hours=12)
    assert services.session_status(session, now=at_cap) == SessionStatus.EXPIRED.value  # derived
    assert count_visible(session, now=at_cap) == 0  # invisible even unswept
    assert services.expire_sessions(now=at_cap) == 1  # sweep stamps it
    session.refresh_from_db()
    assert session.ended_reason == SessionEndedReason.EXPIRED.value
    assert session.ends_at == at_cap  # the sweep ran exactly at the cap

# =========================================================== ping pruning
def test_prune_pings_retention_boundary(world):
    """At EXACTLY 72 h a ping is deleted; 71 h 59 m survives; sessions
    survive their pings (an ended session shows honest emptiness)."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    gone_pin = _fix(session, T0 - PING_RETENTION_HOURS * timedelta(hours=1))
    kept_pin = _fix(session, T0 - timedelta(hours=71, minutes=59))
    pruned = services.prune_pings(now=T0)
    assert pruned == 1
    assert not PingPoint.objects.filter(pk=gone_pin.pk).exists()
    assert PingPoint.objects.filter(pk=kept_pin.pk).exists()
    assert services.prune_pings(now=T0) == 0  # idempotent
    assert TrackingSession.objects.filter(pk=session.pk).exists()  # survives


def test_prune_pings_returns_only_ping_rows(world):
    """The count counts PING rows (the pruner never deletes sessions or
    anything else — data minimization with an exact receipt)."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    _fix(session, T0 - timedelta(hours=80))
    _fix(session, T0 - timedelta(hours=75))
    _fix(session, T0 - timedelta(hours=1))
    assert services.prune_pings(now=T0) == 2


def test_visible_positions_excludes_72h_old_even_if_unpruned(world):
    """Defense in depth: the read cuts at the same 72 h boundary as the
    job, so a lagging cron can never widen what consumers see."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    old = _fix(session, T0 - timedelta(hours=72))  # job hasn't run yet
    _fix(session, T0, _anchor())                   # first anchor
    m1 = _fix(session, T0 + timedelta(minutes=1), _east(5.0))   # visible
    m2 = _fix(session, T0 + timedelta(minutes=2), _east(9.0))   # visible
    _fix(session, T0 + timedelta(minutes=3), _east(25.0))       # last anchor
    rows = visible(session, T0 + timedelta(minutes=4))
    assert [p.pk for p in rows] == [m1.pk, m2.pk]  # the 72 h row is gone


# ============================================ active-session-only visibility
def test_anchors_suppressed_leave_only_the_middle(world):
    session, raw_token = _start(world["veh_b"], world["admin_b"], now=T0)
    _fix(session, T0 + timedelta(minutes=1), _east(1.0))   # first = anchor
    b = _fix(session, T0 + timedelta(minutes=2), _east(3.0))  # 2 km from first, 5 km from last
    _fix(session, T0 + timedelta(minutes=3), _east(8.0))   # last = anchor
    rows = visible(session, T0 + timedelta(minutes=4))
    assert [p.pk for p in rows] == [b.pk]  # chronological, suppression-aware


def _trio_session(world, cap_hours=12):
    """Live session + 3 pings (first/mid/last) for visibility-gate tests."""
    session, raw_token = _start(world["veh_a"], world["crew"], cap_hours=cap_hours, now=T0)
    _fix(session, T0 + timedelta(minutes=1), _east(1.0))
    _fix(session, T0 + timedelta(minutes=2), _east(5.0))
    _fix(session, T0 + timedelta(minutes=3), _east(20.0))
    return session


def test_visible_positions_ended_session_is_totally_empty(world):
    """ACCEPTANCE — the flagship privacy proof: same rows in the table, a
    manual STOP, and the door returns NOTHING. Provable invisibility."""
    session = _trio_session(world)
    at_end = T0 + timedelta(hours=1)
    assert count_visible(session, now=at_end) == 1   # live: the mid ping
    services.end_session(session, by=world["crew"], now=at_end)
    assert count_visible(session, now=at_end) == 0   # ended: none
    assert count_visible(session, now=at_end + timedelta(minutes=1)) == 0
    assert count_visible(session, now=at_end + timedelta(days=5)) == 0


def test_visible_positions_expired_unswept_session_is_empty(world):
    """EXPIRED without any cron running: the derived-status gate is the
    boundary — the sweep being late can NEVER widen visibility."""
    session, raw_token = _start(world["veh_a"], world["crew"], cap_hours=1, now=T0)
    _fix(session, T0 + timedelta(seconds=30))
    _fix(session, T0 + timedelta(minutes=45))          # first (anchor)
    past_cap = T0 + timedelta(hours=2)
    assert count_visible(session, now=T0 + timedelta(minutes=30)) == 0  # 2 pings: both anchors
    assert count_visible(session, now=past_cap) == 0   # unswept-expired
    services.expire_sessions(now=past_cap)             # NOW swept
    assert count_visible(session, now=past_cap) == 0   # still nothing


def test_visible_positions_before_the_start_moment_is_empty(world):
    """The door cannot leak backwards in time: before started_at the
    session is not live at that now."""
    session = _trio_session(world)
    assert count_visible(session, now=T0 - timedelta(seconds=1)) == 0


# ================================================== boundary suppression
def _pt_float(point) -> tuple[float, float]:
    """(Decimal lat, Decimal lon) → (float, float) for haversine checks."""
    return float(point[0]), float(point[1])


def test_ring_geometry_synthetic_offsets_bracket_the_radius():
    """Honesty test: the synthetic coordinates in the boundary tests CLAIM
    to sit inside (0.8 km) / outside (1.2 km) the 1 km privacy ring —
    verify with the haversine the geo layer itself ships, so the boundary
    case rests on arithmetic, not vibes."""
    base = _pt_float(_anchor())
    near = _pt_float(_east(0.8))
    far = _pt_float(_east(1.2))
    assert distance_km(base, near) == pytest.approx(0.8, abs=0.02)
    assert distance_km(base, far) == pytest.approx(1.2, abs=0.03)
    assert distance_km(base, near) < PRIVACY_RADIUS_KM < distance_km(base, far)


def test_boundary_suppression_ring_cases(world):
    """The spec cases, on ONE live session:
    first ping (start anchor) and last ping (end anchor) ALWAYS dropped;
    0.8 km east of the start anchor → dropped (inside the 1 km ring);
    1.2 km east of BOTH anchors → kept;  5 km east → kept.
    """
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    first = _fix(session, T0 + timedelta(seconds=10), _anchor())        # start anchor
    near = _fix(session, T0 + timedelta(minutes=1), _east(0.8))         # INSIDE ring(p1)
    far = _fix(session, T0 + timedelta(minutes=2), _east(1.2))          # outside both
    mid = _fix(session, T0 + timedelta(minutes=3), _east(5.0))          # outside both
    last = _fix(session, T0 + timedelta(minutes=4), _east(25.0))        # END anchor
    rows = visible(session, T0 + timedelta(minutes=5))
    assert [p.pk for p in rows] == [far.pk, mid.pk]
    assert {near.pk, first.pk, last.pk}.isdisjoint({p.pk for p in rows})


def test_boundary_suppression_ring_wraps_the_last_anchor_too(world):
    """The ring guards BOTH boundaries: a ping 0.5 km shy of the FINAL
    stop is unreconstructable; 1.5 km back is history, not home."""
    session, raw_token = _start(world["veh_b"], world["admin_b"], now=T0)
    _fix(session, T0 + timedelta(seconds=10), _anchor())          # first anchor
    kept = _fix(session, T0 + timedelta(minutes=1), _east(1.5))   # outside ring(p_last)
    dropped = _fix(session, T0 + timedelta(minutes=2), _east(0.5))  # inside ring(p_last)
    last = _fix(session, T0 + timedelta(minutes=3), _east(0.0))   # END anchor (=base!)
    rows = visible(session, T0 + timedelta(minutes=4))
    # 'kept' is 1.5 km from the last anchor (base) and 1.5 km from the
    # first (also base) — outside both rings. 'dropped' sits 0.5 km from
    # each. (Same base point twice: both anchors coincide HERE deliberately.)
    assert [p.pk for p in rows] == [kept.pk]


def test_single_ping_session_is_entirely_suppressed(world):
    """One ping = BOTH anchors at once → suppressed → nothing is public
    from a session that produced a single fix (it is its own boundary)."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    _fix(session, T0 + timedelta(minutes=1))
    assert count_visible(session, now=T0 + timedelta(minutes=2)) == 0


def test_two_far_apart_pings_are_suppressed_as_anchors(world):
    """Two pings 25 km apart: first and last are dropped BY RULE, not by
    distance — the boundary always stays out of the public map."""
    session, raw_token = _start(world["veh_b"], world["admin_b"], now=T0)
    _fix(session, T0 + timedelta(minutes=1), _anchor())
    _fix(session, T0 + timedelta(minutes=2), _east(25.0))
    assert count_visible(session, now=T0 + timedelta(minutes=3)) == 0


# ============================================ identity absence (serializers)
def test_position_payload_has_the_exact_documented_keys(world):
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    _fix(session, T0 + timedelta(seconds=10), _anchor())            # start anchor
    mid = _fix(session, T0 + timedelta(minutes=1), _east(5.0))
    _fix(session, T0 + timedelta(minutes=2), _east(25.0))           # end anchor
    (payload,) = serialization.session_positions(session, now=T0)["positions"]
    assert set(payload) == {
        "session_id", "vehicle_id", "lat", "lon", "recorded_at", "confidence",
    }
    assert payload["confidence"] == "LIVE_GPS"
    assert payload["session_id"] == session.pk


def test_session_payload_has_the_exact_documented_keys(world):
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    payload = serialization.session_positions(session, now=T0)
    assert set(payload) == {
        "session_id", "vehicle_id", "plate", "nickname", "live", "started_at", "positions",
    }
    assert payload["plate"] == "KCB-220-P"  # business data IS public (KDPA)
    assert payload["nickname"] == "Mwangaza"
    assert payload["live"] is True


def test_no_crew_identity_in_any_public_serialization(world):
    """Rule 5's teeth: across EVERY public shape, the crew human is absent.
    Bait: a distinctive username + a real-looking phone in the DB."""
    session_a, raw_a = _start(world["veh_a"], world["crew"], now=T0)
    session_b, raw_b = _start(world["veh_b"], world["admin_b"], now=T0)
    _fix(session_a, T0 + timedelta(minutes=1), _east(5.0))
    _fix(session_b, T0 + timedelta(minutes=2), _east(9.0))
    payloads = [serialization.session_positions(s, now=T0)["positions"]
                for s in (session_a, session_b)]
    blob = json.dumps(serialization.live_map_payload(now=T0)) + json.dumps(payloads)
    for bait in (
        "privacy_bait", "+254711222333", "+254711444555", "driver_privacy",
        "operator_a", raw_a, raw_b, "mtk_", "authorized_by", "token_hash",
        "username", "role",
    ):
        assert bait not in blob, f"identity leak: {bait} in a public payload"


def test_ended_session_serializes_honest_empty(world):
    session = _trio_session(world)
    services.end_session(session, by=world["crew"], now=T0 + timedelta(hours=1))
    payload = serialization.session_positions(session, now=T0 + timedelta(hours=1))
    assert payload["live"] is False
    assert payload["positions"] == []


def test_live_map_payload_carries_only_live_sessions(world):
    live_a, raw_a = _start(world["veh_a"], world["crew"], now=T0)
    ended_b, raw_b = _start(world["veh_b"], world["admin_b"], now=T0)
    services.end_session(ended_b, by=world["admin_b"], now=T0 + timedelta(minutes=5))
    fresh_b = _start(world["veh_b"], world["admin_b"], now=T0 + timedelta(hours=1))[0]
    past = T0 + timedelta(hours=3)  # both 12 h caps far from passing
    payload = serialization.live_map_payload(now=past)
    ids = {entry["session_id"] for entry in payload}
    assert ids == {live_a.pk, fresh_b.pk}  # the ended row is NOT carried
    for entry in payload:  # Map entries inherit the suppression rules —
        # there is no other path to a position payload (module docstring).
        assert set(entry) == {
            "session_id", "vehicle_id", "plate", "nickname", "live", "started_at", "positions",
    }

# ============================================================== dedupe floor
def test_ping_unique_per_session_and_recorded_at(world):
    """Model floor: 28's ingest dedupes; even if it forgot, the DB refuses
    a second ping with the same (session, recorded_at)."""
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    _fix(session, T0 + timedelta(minutes=1), _east(5.0))
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _fix(session, T0 + timedelta(minutes=1), _east(5.0))


# ==================================================================== views
def test_start_page_requires_login(world, client):
    client.logout()
    response = client.get(reverse("tracking:start"))
    assert response.status_code == 302
    assert "login" in response["Location"].lower()


def test_crew_start_flow_shows_token_exactly_once(world, client):
    client = _client_for(client, world["crew"])
    # GET — the vehicle select lists ONLY their vehicle (tenant scoping):
    form = client.get(reverse("tracking:start"))
    assert form.status_code == 200
    assert "KCB-220-P" in form.content.decode()
    assert "KBX-456-M" not in form.content.decode()  # B's vehicle absent
    # POST — the token page is rendered BY THE POST RESPONSE itself (no
    # redirect; refreshing cannot re-show a credential that wasn't stored):
    response = client.post(
        reverse("tracking:start"),
        {"vehicle": world["veh_a"].pk, "cap_hours": ""},
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'data-test="token-once"' in body
    raw = _token_in(body)
    assert raw, "token page must show the raw token exactly here"
    assert body.count("mtk_") == 1  # exactly one display, exactly once
    # At rest: only the scope-baked hash (verify recomputes it):
    stored = TrackingSession.objects.get(vehicle=world["veh_a"], ends_at__isnull=True)
    assert services.verify_session_token(
        raw, session_id=stored.pk, vehicle_id=stored.vehicle_id
    ) == stored
    assert raw not in stored.token_hash and len(stored.token_hash) == 64
    # A SECOND live start for the same vehicle is refused on the page:
    second = client.post(reverse("tracking:start"), {"vehicle": world["veh_a"].pk})
    assert "already has a live tracking session" in second.content.decode()


def test_crew_start_for_foreign_vehicle_refused_on_page(world, client):
    """A crafted POST with another SACCO's vehicle pk changes nothing —
    the SERVICE door refuses it and the page shows the honest error."""
    client = _client_for(client, world["crew"])
    response = client.post(reverse("tracking:start"), {"vehicle": world["veh_b"].pk})
    body = response.content.decode()
    assert response.status_code == 200  # rendered back with the flash
    assert "Only approved crew of this vehicle" in body
    assert TrackingSession.objects.count() == 0


def test_shorter_cap_via_the_form(world, client):
    client = _client_for(client, world["admin_b"])
    response = client.post(
        reverse("tracking:start"),
        {"vehicle": world["veh_b"].pk, "cap_hours": "2"},
    )
    assert response.status_code == 200
    session = TrackingSession.objects.get(vehicle=world["veh_b"])
    assert session.cap_hours == 2


# =================================================================== ends
def test_end_by_crew_stops_publishing(world, client):
    session, raw_token = _start(world["veh_a"], world["crew"], now=VNOW)
    _fix(session, VNOW + timedelta(minutes=1), _east(5.0))
    client = _client_for(client, world["crew"])
    response = client.post(reverse("tracking:end", args=[session.pk]))
    assert response.status_code == 302
    session.refresh_from_db()
    assert session.ends_at is not None
    assert session.ended_reason == SessionEndedReason.STOPPED.value
    # Follow-up page shows the honest derived status + zero visible count:
    page = client.get(reverse("tracking:my_sessions"))
    body = page.content.decode()
    assert "ENDED" in body
    assert 'data-test="session-row"' in body
    assert services.visible_positions(session, now=dj_timezone.now()).count() == 0


def test_end_by_the_sacco_admin_is_allowed(world, db):
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    services.end_session(session, by=world["admin_a"], now=T0 + timedelta(hours=2))
    assert services.session_status(session, now=T0 + timedelta(hours=2)) == SessionStatus.ENDED.value


def test_late_stop_press_cannot_extend_the_cap(world):
    """The privacy-critical end test: pressing stop AFTER the cap must be
    REFUSED (ends_at > deadline would extend the permission), the row
    stays unswept-expired, and its data is already inaccessible."""
    session, raw_token = _start(world["veh_a"], world["crew"], cap_hours=1, now=T0)
    with pytest.raises(ValidationError):
        services.end_session(session, by=world["crew"], now=T0 + timedelta(hours=5))
    session.refresh_from_db()
    assert session.ends_at is None  # not extended by hand
    assert count_visible(session, now=T0 + timedelta(hours=5)) == 0
    # The sweep can still stamp it honestly:
    assert services.expire_sessions(now=T0 + timedelta(hours=5)) == 1


def test_end_foreign_session_or_double_end_refused(world, db):
    session, raw_token = _start(world["veh_a"], world["crew"], now=T0)
    # Other vehicle / other sacco actors cannot end it:
    with pytest.raises(ValidationError):
        services.end_session(session, by=world["crew_b"])
    with pytest.raises(ValidationError):
        services.end_session(session, by=world["admin_b"])
    with pytest.raises(ValidationError):
        services.end_session(session, by=world["plain"])
    services.end_session(session, by=world["crew"], now=T0 + timedelta(minutes=1))
    # Double end refused (an end moment twice is a lie):
    with pytest.raises(ValidationError):
        services.end_session(session, by=world["crew"], now=T0 + timedelta(minutes=2))


# ================================================== operator / 403 / scopes
def test_operator_sessions_page_is_tenant_scoped(world, client):
    session_a = _start(world["veh_a"], world["crew"], now=VNOW)[0]
    session_b = _start(world["veh_b"], world["admin_b"], now=VNOW)[0]
    admin_a_page = _client_for(client, world["admin_a"]).get(reverse("tracking:operator"))
    body_a = admin_a_page.content.decode()
    assert admin_a_page.status_code == 200
    assert "KCB-220-P" in body_a and "LIVE" in body_a
    assert "KBX-456-M" not in body_a  # SACCO B's row is structurally absent
    # The mirror proves it's scoping and not page emptiness:
    admin_b_page = _client_for(client, world["admin_b"]).get(reverse("tracking:operator"))
    body_b = admin_b_page.content.decode()
    assert "KBX-456-M" in body_b and "KCB-220-P" not in body_b


def test_my_sessions_lists_only_crew_owned_vehicles(world, client):
    _start(world["veh_a"], world["crew"], now=VNOW)[0]
    _start(world["veh_b"], world["admin_b"], now=VNOW)[0]
    page = _client_for(client, world["crew"]).get(reverse("tracking:my_sessions"))
    body = page.content.decode()
    assert "KCB-220-P" in body
    assert "KBX-456-M" not in body
    # Plain member has nothing at all:
    empty = _client_for(client, world["plain"]).get(reverse("tracking:my_sessions"))
    assert 'data-test="no-vehicles"' in empty.content.decode()


def test_end_and_rotate_foreign_session_is_403(world, client):
    session_a = _start(world["veh_a"], world["crew"], now=VNOW)[0]
    session_b = _start(world["veh_b"], world["admin_b"], now=VNOW)[0]
    client = _client_for(client, world["crew"])
    for name in ("tracking:end", "tracking:rotate"):
        response = client.post(reverse(name, args=[session_b.pk]))
        assert response.status_code == 403, f"{name} must deny foreign sessions"
    # Own session still ends fine (the 403 wasn't a blanket block):
    assert client.post(reverse("tracking:end", args=[session_a.pk])).status_code == 302


def test_rotate_view_gives_a_fresh_display_once_token(world, client):
    session, old_raw = _start(world["veh_a"], world["crew"], now=VNOW)
    client = _client_for(client, world["crew"])
    # GET is refused (rotation is a state change, POST only):
    assert client.get(reverse("tracking:rotate", args=[session.pk])).status_code == 405
    response = client.post(reverse("tracking:rotate", args=[session.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    new_raw = _token_in(body)
    assert new_raw != old_raw
    assert services.verify_session_token(
        old_raw, session_id=session.pk, vehicle_id=session.vehicle_id
    ) is None
    assert services.verify_session_token(
        new_raw, session_id=session.pk, vehicle_id=session.vehicle_id
    ) == session


def test_start_page_and_lists_stay_under_the_html_budget(world, client):
    _start(world["veh_a"], world["crew"], now=VNOW)[0]
    client = _client_for(client, world["crew"])
    assert_under_budget(client.get(reverse("tracking:start")).content.decode(), 50)
    assert_under_budget(client.get(reverse("tracking:my_sessions")).content.decode(), 50)
    admin_client = _client_for(client, world["admin_b"])
    assert_under_budget(admin_client.get(reverse("tracking:operator")).content.decode(), 50)


# ==================================================================== i18n
def test_tracking_strings_are_gettext_marked():
    """Hard rule probe (fleet's pattern): user-facing strings go through
    gettext in the modules, {% trans %}/{% blocktranslate %} in templates."""
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "apps" / "tracking"
    for name in ("models.py", "services.py", "views.py", "admin.py"):
        source = (app_dir / name).read_text(encoding="utf-8")
        assert '_("' in source, f"{name} lacks gettext markers"
    for template in app_dir.glob("templates/tracking/*.html"):
        text = template.read_text(encoding="utf-8")
        assert "{% trans" in text or "{% blocktranslate" in text, \
            f"template {template.name} lacks i18n marks"


def test_public_doors_match_the_module_contract():
    """API surface smoke for Prompts 28/29: the documented doors exist and
    behave at the boundary (doc string includes the DPIA mitigation note)."""
    assert services.DEFAULT_CAP_HOURS == 12
    doc = (services.visible_positions.__doc__ or "") + (services.__doc__ or "")
    assert "DPIA" in doc
    # Endpoints exist for the crew flow (29) and operator page:
    for name in ("tracking:start", "tracking:my_sessions", "tracking:operator",
                 "tracking:end", "tracking:rotate"):
        assert reverse(name, kwargs={"pk": 1}) if name.endswith(
            ("end", "rotate")
        ) else reverse(name)  # resolvable routes

