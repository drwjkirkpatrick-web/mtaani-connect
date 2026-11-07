"""tests/test_ingest.py — Prompt 28: location ingestion (batched, token auth).

Coverage (injected clock + Prompt 27's public doors everywhere):

  * batch of ≤50 ingested fully — rows written, received_at = the FLUSH
    moment (never the device stamp), count arithmetic exact;
  * over-batch → honest refusal, NOTHING written (never a 500);
  * dedupe on (session, recorded_at): repeats inside one flush counted, a
    flushed moment already stored counted, a replayed flush all-duplicates —
    and NO double rows (27's unique floor never fires);
  * plausibility: an impossible jump (10 km in 2 min ⇒ ~300 km/h implied) →
    row KEPT with confidence=ESTIMATE (never deleted) + speed explaining
    the label; exactly 150 reported stays LIVE, 151 downgrades; the 60–150
    band stays LIVE_GPS; a 200 km/h report downgrades a near-stationary fix;
  * pure implied_speed_kmh: exact boundary, backward steps unjudged;
  * the uniform credential refusal: empty token / wrong token / right token
    claimed for another session / token against a NONEXISTENT session —
    every one raises THE SAME refusal, no rows, no existence oracle;
  * ingest on an ENDED session (and an unswept EXPIRED one) → plain refusal;
  * the view: POST+header round-trip, JSON body/responses, no login cookie
    needed, a wrong header name fails uniformly, GET → 405 JSON;
  * Prompt 27 regressions: serializer shapes exact with the new columns,
    visible_positions still rules over ingest-written rows.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.core.geo import distance_km
from apps.core.testing import mock_now
from apps.core.types import Confidence
from apps.fleet import services as fleet_services
from apps.routes.models import Sacco
from apps.tracking import ingest, serialization, services
from apps.tracking.models import PingPoint

T0 = mock_now()  # the injected clock — every call names now= explicitly
FLUSH_AT = T0 + timedelta(minutes=2)  # when the device flushes the batch

# Deterministic synthetic coordinates: points EAST along the CBD's constant
# latitude; the DEG constant is verified against the haversine the code
# uses (test below), so every threshold claim rests on arithmetic.
BASE = (-1.286400, 36.817200)  # Nairobi CBD (lat, lon)
DEG_LON_KM = 111.32 * 0.999748  # 111.32 km/deg × cos(radians(1.2864°))


def _east(km: float) -> tuple[Decimal, Decimal]:
    """A synthetic (lat, lon) point `km` EAST of the anchor, 6-dp safe."""
    dlon = Decimal(km / DEG_LON_KM).quantize(Decimal("0.000001"))
    return Decimal(str(BASE[0])), Decimal(str(BASE[1])) + dlon


def _ping_item(at, east_km: float = 0.0, speed=None) -> dict:
    lat, lon = _east(east_km)
    item = {"recorded_at": at.isoformat(), "lat": float(lat), "lon": float(lon)}
    if speed is not None:
        item["speed_kmh"] = speed
    return item


def _ingest(target, token, items, *, at=FLUSH_AT) -> dict:
    """The service door under the fixed clock (tests assert on its counts)."""
    return ingest.ingest_pings(
        token, target.pk, target.vehicle_id, items, now=at
    )


def _refused(call) -> str | None:
    """Raise-on-refusal helper: CALLS the thunk, returns its refusal MESSAGE.

    (SessionAuthRefused subclasses ValidationError, so one except covers it —
    the service contract refuses via ValidationError vocabulary throughout.)
    """
    try:
        call()
    except ValidationError as exc:
        return str(exc.message) if exc.message is not None else "; ".join(exc.messages)
    return None  # pragma: no cover — callers assert the refusal happened


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world(db):
    """SACCO A (admin_a + crew + veh_a) and SACCO B (admin_b + crew_b +
    veh_b) — the wrong-scope tests claim across that wall and must fail."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_t28", "staff@example.com", "***")
    sacco_a = Sacco.objects.create(name="Ingest United")
    sacco_b = Sacco.objects.create(name="Other Sacco")
    admin_a = User.objects.create_user("op_t28", "op@example.com", "***")
    admin_b = User.objects.create_user("op_t28b", "opb@example.com", "***")
    crew = User.objects.create_user("driver_t28", "crew@example.com", "***")
    crew_b = User.objects.create_user("driver_b_t28", "crew_b@example.com", "***")
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
    return {
        "staff": staff, "sacco_a": sacco_a, "sacco_b": sacco_b,
        "admin_a": admin_a, "admin_b": admin_b,
        "crew": crew, "crew_b": crew_b, "veh_a": veh_a, "veh_b": veh_b,
    }


@pytest.fixture
def live_session(world):
    """A LIVE session on vehicle A (crew started it at T0, default cap)."""
    session, raw_token = services.start_session(
        world["veh_a"], by=world["crew"], now=T0
    )
    return {"session": session, "raw_token": raw_token}


REAL_NOW = dj_timezone.now()  # the clock the VIEWS inject — view tests use it


@pytest.fixture
def live_now(world):
    """A LIVE session started at the REAL clock: view flows inject
    timezone.now() (not T0), so their sessions must be live NOW.""" 
    session, raw_token = services.start_session(world["veh_a"], by=world["crew"])
    return {"session": session, "raw_token": raw_token, "base": REAL_NOW}


def _item_at(base, minutes: float, east_km: float = 0.0, speed=None) -> dict:
    at = base + timedelta(minutes=minutes)
    if speed is None:
        return _ping_item(at, east_km=east_km)
    return _ping_item(at, east_km=east_km, speed=speed)


# ======================================================= batch + clean counts
def test_full_batch_of_fifty_ingested(world, live_session):
    items = [
        _ping_item(T0 + timedelta(minutes=1, seconds=i), east_km=i * 0.03, speed=42.0)
        for i in range(50)
    ]
    result = _ingest(live_session["session"], live_session["raw_token"], items)
    assert result == {
        "accepted": 50, "downgraded": 0, "duplicates": 0,
        "batch_limit": 50,
        "session_id": live_session["session"].pk,
        "vehicle_id": live_session["session"].vehicle_id,
    }
    rows = list(PingPoint.objects.filter(session=live_session["session"]))
    assert len(rows) == 50
    assert rows[0].recorded_at == T0 + timedelta(minutes=1)
    assert rows[0].received_at == FLUSH_AT  # flush arrival, not device stamp
    assert rows[0].confidence == Confidence.LIVE_GPS.value
    assert rows[0].speed_kmh == Decimal("42.00")


def test_ingest_without_reported_speed_keeps_the_column_null(world, live_session):
    result = _ingest(
        live_session["session"], live_session["raw_token"],
        [_ping_item(T0 + timedelta(minutes=1))],
    )
    assert result["accepted"] == 1
    row = PingPoint.objects.get(session=live_session["session"])
    assert row.speed_kmh is None  # unknown ≠ 0.0 — absence stays absence


def test_empty_batch_is_an_honest_noop(world, live_session):
    result = _ingest(live_session["session"], live_session["raw_token"], [])
    assert result == {
        "accepted": 0, "downgraded": 0, "duplicates": 0,
        "batch_limit": 50,
        "session_id": live_session["session"].pk,
        "vehicle_id": live_session["session"].vehicle_id,
    }
    assert PingPoint.objects.count() == 0


# ================================================================ over-batch
def test_over_batch_refused_before_anything_is_written(world, live_session):
    items = [_ping_item(T0 + timedelta(minutes=1, seconds=i)) for i in range(51)]
    message = _refused(lambda: _ingest(
        live_session["session"], live_session["raw_token"], items
    ))
    assert message is not None and "50" in message
    assert PingPoint.objects.count() == 0  # honest refusal wrote NOTHING


def test_boundary_batch_of_exactly_fifty_is_ok(world, live_session):
    items = [_ping_item(T0 + timedelta(minutes=1, seconds=i)) for i in range(50)]
    assert _ingest(
        live_session["session"], live_session["raw_token"], items
    )["accepted"] == 50


# ==================================================================== dedupe
def test_in_batch_duplicate_counted_not_written(world, live_session):
    items = [
        _ping_item(T0 + timedelta(minutes=1), east_km=0.03),
        _ping_item(T0 + timedelta(minutes=1), east_km=0.03),  # same moment
        _ping_item(T0 + timedelta(minutes=2), east_km=0.06),
    ]
    result = _ingest(live_session["session"], live_session["raw_token"], items)
    assert result["accepted"] == 2
    assert result["duplicates"] == 1
    assert PingPoint.objects.count() == 2  # no double row, no 500


def test_flush_colliding_with_a_stored_moment_is_a_duplicate(world, live_session):
    lat, lon = _east(0.03)
    PingPoint.objects.create(  # the previous fix, already stored
        session=live_session["session"], lat=lat, lon=lon,
        recorded_at=T0 + timedelta(minutes=1),
        received_at=T0 + timedelta(seconds=40),
    )
    items = [
        _ping_item(T0 + timedelta(minutes=1), east_km=0.03),  # known moment
        _ping_item(T0 + timedelta(minutes=2), east_km=0.06),
        _ping_item(T0 + timedelta(minutes=3), east_km=0.09),
    ]
    result = _ingest(live_session["session"], live_session["raw_token"], items)
    assert result["accepted"] == 2
    assert result["duplicates"] == 1
    assert PingPoint.objects.count() == 3  # the stored row was NOT rewritten


def test_replayed_flush_counts_all_duplicates(world, live_session):
    items = [_ping_item(T0 + timedelta(minutes=1 + i, seconds=i)) for i in range(5)]
    first = _ingest(live_session["session"], live_session["raw_token"], items)
    assert first["accepted"] == 5
    replay = _ingest(  # the offline queue flushed again after reconnect
        live_session["session"], live_session["raw_token"], items
    )
    assert replay["accepted"] == 0
    assert replay["downgraded"] == 0
    assert replay["duplicates"] == 5
    assert PingPoint.objects.count() == 5


def test_mixed_batch_count_arithmetic_is_honest(world, live_session):
    lat, lon = _east(0.03)
    PingPoint.objects.create(
        session=live_session["session"], lat=lat, lon=lon,
        recorded_at=T0 + timedelta(minutes=9), received_at=T0,
    )
    items = [
        _ping_item(T0 + timedelta(minutes=9), east_km=0.03),   # db duplicate
        _ping_item(T0 + timedelta(minutes=10), east_km=0.06),  # live keep
        _ping_item(T0 + timedelta(minutes=10), east_km=0.06),  # batch duplicate
        _ping_item(T0 + timedelta(minutes=11), east_km=10.0),  # impossible jump
    ]
    result = _ingest(live_session["session"], live_session["raw_token"], items)
    assert result["accepted"] == 1
    assert result["downgraded"] == 1
    assert result["duplicates"] == 2  # sums: 1+1+2 == len(items) == 4


# ============================================================== plausibility
def test_impossible_jump_downgraded_and_KEPT_not_deleted(world, live_session):
    """10 km east in 2 min ≈ 300 km/h — implausible for a matatu → the row
    survives, labeled ESTIMATE, with the implied speed explaining the label."""
    items = [
        _ping_item(T0 + timedelta(minutes=1), east_km=0.0, speed=50.0),
        _ping_item(T0 + timedelta(minutes=3), east_km=10.0),
    ]
    result = _ingest(live_session["session"], live_session["raw_token"], items)
    assert result["accepted"] == 1 and result["downgraded"] == 1
    row = PingPoint.objects.get(
        session=live_session["session"], recorded_at=T0 + timedelta(minutes=3)
    )
    assert row.confidence == Confidence.ESTIMATE.value   # DOWNGRADE, KEPT
    assert float(row.speed_kmh) == pytest.approx(300.0, abs=0.6)
    first = PingPoint.objects.get(
        session=live_session["session"], recorded_at=T0 + timedelta(minutes=1)
    )
    assert first.confidence == Confidence.LIVE_GPS.value  # no predecessor yet


def test_reported_150_exactly_stays_live_151_downgrades(world, live_session):
    """Exact reported-speed boundary (spec: speed > 150 → downgrade)."""
    result = _ingest(
        live_session["session"], live_session["raw_token"], [
            _ping_item(T0 + timedelta(minutes=1), east_km=0.03, speed=150.0),
            _ping_item(T0 + timedelta(minutes=2), east_km=0.03, speed=151.0),
        ]
    )
    assert result["accepted"] == 1 and result["downgraded"] == 1
    assert PingPoint.objects.get(
        session=live_session["session"], recorded_at=T0 + timedelta(minutes=1)
    ).confidence == Confidence.LIVE_GPS.value
    assert PingPoint.objects.get(
        session=live_session["session"], recorded_at=T0 + timedelta(minutes=2)
    ).confidence == Confidence.ESTIMATE.value


def test_band_60_to_150_stays_live_gps(world, live_session):
    """2 km and 4.8 km per 2 min ⇒ ≈60 and ≈144 km/h: the plausible band."""
    result = _ingest(
        live_session["session"], live_session["raw_token"], [
            _ping_item(T0 + timedelta(minutes=1), east_km=0.0, speed=60.0),
            _ping_item(T0 + timedelta(minutes=3), east_km=2.0, speed=90.0),
            _ping_item(T0 + timedelta(minutes=5), east_km=6.8, speed=120.0),
        ]
    )
    assert result["accepted"] == 3 and result["downgraded"] == 0
    rows = list(PingPoint.objects.filter(session=live_session["session"]))
    assert all(r.confidence == Confidence.LIVE_GPS.value for r in rows)


def test_reported_200_downgrades_a_near_stationary_fix(world, live_session):
    """The device CLAIMS 200 km/h while barely moving: spec verbatim applies
    to the report too — fix kept, labeled ESTIMATE, claim stored."""
    result = _ingest(
        live_session["session"], live_session["raw_token"], [
            _ping_item(T0 + timedelta(minutes=1), east_km=0.03, speed=55.0),
            _ping_item(T0 + timedelta(minutes=2), east_km=0.035, speed=200.0),
        ]
    )
    assert result["downgraded"] == 1
    row = PingPoint.objects.get(
        session=live_session["session"], recorded_at=T0 + timedelta(minutes=2)
    )
    assert row.confidence == Confidence.ESTIMATE.value
    assert row.speed_kmh == Decimal("200.00")


def test_chain_spans_flushes_via_the_stored_tail(world, live_session):
    """Fix A lands in flush 1; the 4 km/min move in flush 2 is judged
    against A (the stored tail), not against nothing."""
    _ingest(
        live_session["session"], live_session["raw_token"],
        [_ping_item(T0 + timedelta(minutes=1), east_km=0.0)],
    )
    later = _ingest(
        live_session["session"], live_session["raw_token"],
        [_ping_item(T0 + timedelta(minutes=4), east_km=12.0)],
    )
    assert later["downgraded"] == 1


def test_backwards_late_item_is_not_falsely_judged(world, live_session):
    """A late-arriving fix OLDER than the stored tail: no forward math is
    possible → the review cannot judge it → LIVE (no invented verdict),
    and the NEXT item chains from it."""
    lat, lon = _east(0.0)
    PingPoint.objects.create(
        session=live_session["session"], lat=lat, lon=lon,
        recorded_at=T0 + timedelta(minutes=10), received_at=T0,
    )
    result = _ingest(
        live_session["session"], live_session["raw_token"], [
            _ping_item(T0 + timedelta(minutes=4), east_km=6.0),   # OLD, late
            _ping_item(T0 + timedelta(minutes=5), east_km=6.05),  # normal next
        ]
    )
    assert result["accepted"] == 2 and result["downgraded"] == 0


def test_implied_speed_pure_math_boundary():
    """The pure review: exact 150 is not over; no predecessor and backwards
    time are unjudged (None); forward time gives distance/Δt."""
    t5 = T0 + timedelta(minutes=5)
    base = (float(BASE[0]), float(BASE[1]))
    five_km_east = (base[0], base[1] + 5.0 / DEG_LON_KM)
    d = float(distance_km(base, five_km_east))
    assert d == pytest.approx(5.0, abs=0.02)
    assert ingest.implied_speed_kmh(T0, base, t5, five_km_east) == pytest.approx(
        d * 12.0, rel=0.01
    )
    assert ingest.implied_speed_kmh(None, None, t5, five_km_east) is None
    assert ingest.implied_speed_kmh(t5, base, T0, five_km_east) is None
    assert ingest.implied_speed_kmh(T0, base, T0, five_km_east) is None


def test_synthetic_coordinates_meet_their_claim():
    """Honesty floor (27's ring-test pattern): the helpers really produce
    the distances every verdict above claims."""
    assert float(distance_km(BASE, _east(0.03))) == pytest.approx(0.03, abs=0.02)
    assert float(distance_km(_east(0.0), _east(10.0))) == pytest.approx(10.0, abs=0.05)


# ============================================================== refusal sets
def test_wrong_credentials_are_UNIFORM_and_reveal_nothing(world, live_session):
    """Empty token / wrong token / another session's valid token claimed
    here / a valid-shaped token against a NONEXISTENT session — all raise
    THE SAME refusal; nothing is written; nothing about existence leaks."""
    other_session, other_raw = services.start_session(
        world["veh_b"], by=world["crew_b"], now=T0
    )
    target = live_session["session"]
    attempts = [
        ("", target, target.vehicle_id, []),
        (None, target, target.vehicle_id, []),
        ("mtk_not_a_real_token", target, target.vehicle_id, []),
        (other_raw, target, target.vehicle_id, []),  # valid ELSEWHERE token
        (live_session["raw_token"], 987654, target.vehicle_id, []),  # ghost id
    ]
    outcomes = []
    for token, session, vehicle_id, items in attempts:
        try:
            ingest.ingest_pings(
                token, session, vehicle_id, items, now=FLUSH_AT
            )
        except ValidationError as exc:
            outcomes.append(f"{type(exc).__name__}|{exc.message}")
        else:  # pragma: no cover — the contract refuses every wrong call
            raise AssertionError("a wrong credential was ACCEPTED")
    assert len(outcomes) == 5
    assert set(outcomes) == {outcomes[0]}  # IDENTICAL refusal for all five
    assert "SessionAuthRefused" in outcomes[0]
    assert PingPoint.objects.count() == 0  # nothing written on any path
    # The ghost attempt confirmed nothing: the same body a real session gets.
    del other_session  # (its existence in this test only proves scoping)


def test_ended_session_ingest_is_refused_plainly(world, live_session):
    session = live_session["session"]
    services.end_session(session, by=world["crew"], now=T0 + timedelta(minutes=30))
    message = _refused(lambda: _ingest(
        session, live_session["raw_token"],
        [_ping_item(T0 + timedelta(minutes=31))],
    ))
    assert message is not None
    assert "not publishing live" in message   # plain wording…
    assert "credentials" not in message        # …NOT auth language
    assert PingPoint.objects.count() == 0


def test_unswept_expired_session_is_refused_too(world, live_session):
    """The cap passed without any sweep — the deadline rule already closed
    it; ingest must not write for an authorization that reached its end."""
    short, short_raw = services.start_session(
        world["veh_b"], by=world["crew_b"], cap_hours=1, now=T0
    )
    message = _refused(lambda: ingest.ingest_pings(
        short_raw, short.pk, short.vehicle_id,
        [_ping_item(T0 + timedelta(hours=1, minutes=1))],
        now=T0 + timedelta(hours=1, minutes=5),  # unswept-expired at this now
    ))
    assert message is not None and "not publishing live" in message
    assert PingPoint.objects.count() == 0


# ==================================================================== the view
def test_view_full_roundtrip_json_counts(world, live_now, client):
    url = reverse("tracking:ingest", args=[live_now["session"].pk])
    body = {
        "vehicle_id": live_now["session"].vehicle_id,
        "pings": [
            _item_at(live_now["base"], 3, east_km=0.0, speed=55.0),
            _item_at(live_now["base"], 5, east_km=10.0),  # impossible jump
        ],
    }
    response = client.post(
        url, data=json.dumps(body), content_type="application/json",
        headers={ingest.INGEST_HEADER: live_now["raw_token"]},
    )
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    assert json.loads(response.content) == {
        "accepted": 1, "downgraded": 1, "duplicates": 0,
        "batch_limit": 50,
        "session_id": live_now["session"].pk,
        "vehicle_id": live_now["session"].vehicle_id,
    }


def test_view_accepts_the_right_token_without_any_login(world, live_now):
    """Crew devices authenticate by token: an ANONYMOUS client holding the
    token + vehicle_id is accepted; the wrong one is refused."""
    anon = Client()
    url = reverse("tracking:ingest", args=[live_now["session"].pk])
    body = {
        "vehicle_id": live_now["session"].vehicle_id,
        "pings": [_item_at(live_now["base"], 3)],
    }
    ok = anon.post(url, data=json.dumps(body), content_type="application/json",
                   headers={ingest.INGEST_HEADER: live_now["raw_token"]})
    assert ok.status_code == 200 and json.loads(ok.content)["accepted"] == 1
    wrong = anon.post(url, data=json.dumps(body), content_type="application/json",
                      headers={ingest.INGEST_HEADER: "mtk_wrong"})
    assert wrong.status_code == 403


def test_view_bad_header_name_fails_uniformly(world, live_now, client):
    """Pins THE header name: the token under another header is just an
    absent credential — its 403 body is byte-identical to a wrong token's."""
    url = reverse("tracking:ingest", args=[live_now["session"].pk])
    body = {
        "vehicle_id": live_now["session"].vehicle_id,
        "pings": [_item_at(live_now["base"], 3)],
    }
    absent_header = client.post(
        url, data=json.dumps(body), content_type="application/json",
        headers={"X-Other-Tracking-Token": live_now["raw_token"]},
    )
    wrong_token = client.post(
        url, data=json.dumps(body), content_type="application/json",
        headers={ingest.INGEST_HEADER: "mtk_definitely_wrong"},
    )
    assert absent_header.status_code == wrong_token.status_code == 403
    assert absent_header.content == wrong_token.content


def test_view_uniform_403_is_the_same_for_real_and_ghost(world, live_now, client):
    real = client.post(
        reverse("tracking:ingest", args=[live_now["session"].pk]),
        data=json.dumps({"vehicle_id": 1, "pings": []}),
        content_type="application/json",
        headers={ingest.INGEST_HEADER: "mtk_anything"},
    )
    ghost = client.post(
        reverse("tracking:ingest", args=[777777]),
        data=json.dumps({"vehicle_id": 1, "pings": []}),
        content_type="application/json",
        headers={ingest.INGEST_HEADER: "mtk_anything"},
    )
    assert real.status_code == ghost.status_code == 403
    assert real.content == ghost.content  # no existence oracle
    assert json.loads(ghost.content)["accepted"] == 0


def test_view_refusals_are_json_with_plain_reasons(world, live_now, client):
    url = reverse("tracking:ingest", args=[live_now["session"].pk])
    too_big = {
        "vehicle_id": live_now["session"].vehicle_id,
        "pings": [_item_at(live_now["base"], 1, east_km=0.0)
                  for _ in range(51)],  # (identical moments but the size
        # check runs BEFORE any dedupe/insert — the cap is a hard contract)
    }
    over = client.post(
        url, data=json.dumps(too_big), content_type="application/json",
        headers={ingest.INGEST_HEADER: live_now["raw_token"]},
    )
    assert over.status_code == 400
    assert "50" in json.loads(over.content)["error"]
    malformed = client.post(
        url, data=b"{not json", content_type="application/json",
        headers={ingest.INGEST_HEADER: live_now["raw_token"]},
    )
    assert malformed.status_code == 400
    assert "JSON" in json.loads(malformed.content)["error"]
    bad_item = client.post(
        url,
        data=json.dumps({
            "vehicle_id": live_now["session"].vehicle_id,
            "pings": [{"recorded_at": live_now["base"].isoformat(), "lat": "x"}],
        }),
        content_type="application/json",
        headers={ingest.INGEST_HEADER: live_now["raw_token"]},
    )
    assert bad_item.status_code == 400
    assert "index 0" in json.loads(bad_item.content)["error"]
    assert PingPoint.objects.count() == 0


def test_view_get_is_405_json(world, live_now, client):
    response = client.get(
        reverse("tracking:ingest", args=[live_now["session"].pk]),
        headers={ingest.INGEST_HEADER: live_now["raw_token"]},
    )
    assert response.status_code == 405
    assert response["Content-Type"].startswith("application/json")


# ======================================================= Prompt 27 regression
def test_27_serialization_shapes_unchanged_with_new_columns(world, live_session):
    """Additive columns must not have moved 27's frozen shapes — the ones 28's
    map (and 29) render AS-IS."""
    session = live_session["session"]
    _ingest(session, live_session["raw_token"], [
        _ping_item(T0 + timedelta(minutes=1), east_km=1.0),  # anchor 1
        _ping_item(T0 + timedelta(minutes=2), east_km=5.0),  # visible
        _ping_item(T0 + timedelta(minutes=3), east_km=25.0),  # anchor 2
    ])
    entry = serialization.session_positions(session, now=T0 + timedelta(minutes=4))
    assert set(entry) == {
        "session_id", "vehicle_id", "plate", "nickname", "live", "started_at",
        "positions",
    }
    assert set(entry["positions"][0]) == {
        "session_id", "vehicle_id", "lat", "lon", "recorded_at", "confidence",
    }
    assert len(entry["positions"]) == 1  # boundary suppression STILL rules
    assert entry["positions"][0]["confidence"] == Confidence.LIVE_GPS.value


def test_27_visibility_door_rules_ingest_written_rows(world, live_session):
    """Ingest ADDS rows; the privacy door stays in charge of what is ever
    public (an ended session's rows vanish from every read)."""
    session = live_session["session"]
    _ingest(session, live_session["raw_token"], [
        _ping_item(T0 + timedelta(minutes=1), east_km=1.0),
        _ping_item(T0 + timedelta(minutes=2), east_km=5.0),
        _ping_item(T0 + timedelta(minutes=3), east_km=9.0),
    ])
    live_rows = list(
        services.visible_positions(session, now=T0 + timedelta(minutes=4))
    )
    assert len(live_rows) == 1
    services.end_session(session, by=world["crew"], now=T0 + timedelta(minutes=40))
    assert services.visible_positions(
        session, now=T0 + timedelta(minutes=41)
    ).count() == 0


def test_verify_session_token_still_the_scoped_door(world, live_session):
    session = live_session["session"]
    assert services.verify_session_token(
        live_session["raw_token"], session_id=session.pk,
        vehicle_id=session.vehicle_id,
    ) == session
    assert services.verify_session_token(
        "mtk_garbage", session_id=session.pk, vehicle_id=session.vehicle_id
    ) is None


# ==================================================================== i18n
def test_ingest_strings_are_gettext_marked():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "apps" / "tracking" / "ingest.py"
    ).read_text(encoding="utf-8")
    assert '_("' in source, "ingest.py lacks gettext markers"
