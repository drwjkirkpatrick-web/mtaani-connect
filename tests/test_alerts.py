"""tests/test_alerts.py — Prompt 31: arrival estimates + follow alerts.

Acceptance coverage, provably (injected clock everywhere):

  * radius trigger math — inside / outside / boundary-INCLUSIVE, and the
    honest False for a stage with no coordinates (no fabricated triggers);
  * ETA range arithmetic — the PINNED EXACT pure bounds, then the door-flow
    test turning fixture pings into a "7–12 min"-style range with the
    payload key set frozen (lo_min, hi_min, confidence);
  * insufficient-data SILENCE — live session with too few visible fixes →
    eta None + "Not enough live data to estimate." — never 0, never ∞;
  * ESTIMATE label ALWAYS inside the payload (the pitfall, structural);
  * follow CRUD — nickname resolution (icase exact tiered matching),
    duplicate refused, ambiguous refused with a plain rider message,
    retired metal unresolvable, remove idempotent, radius bounds,
    stage-on-path validation;
  * opt-in/out flow — notify default OFF (scan sends NOTHING), one-tap
    toggle flips it, then the alert fires with the estimate inside;
  * MockPush determinism — two identical scans → identical recorded sends;
  * dedupe window — scan +10 min → still ONE send; +35 min → another;
  * per-follow radius honoured; alert_scan injected clock + adapter;
  * views: anonymous redirect, CRUD one-taps, arrivals page (estimate +
    honest silence in one list) + JSON mirror, page budgets.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from math import cos, pi, radians
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.alerts import eta, notify, services
from apps.alerts.models import ALERT_DEDUPE_MINUTES, DEFAULT_RADIUS_KM, FollowAlert, RouteFollow, VehicleFollow
from apps.core.geo import EARTH_RADIUS_KM, distance_km
from apps.core.testing import assert_under_budget, mock_now
from apps.fleet import services as fleet_services
from apps.routes.models import Route, RouteStage, Sacco, Stage
from apps.tracking import services as tracking_services
from apps.tracking.models import PingPoint

T0 = mock_now()  # one fixed UTC moment — the injected clock rule
# View flows run on the REAL clock (views inject timezone.now()); fixture
# sessions for view tests start at import time so the 12 h cap comfortably
# holds while the request lands seconds later.
VNOW = dj_timezone.now()

# Geometry: pings walk ALONG constant latitude east of the stage; every
# claimed distance is cross-checked with the actual haversine (distance_km).
STAGE_LAT = Decimal("-1.286400")
STAGE_LON = Decimal("36.817200")
# km per degree of longitude AT this latitude — derived from core.geo's own
# EARTH_RADIUS_KM so claimed offsets and measured distances agree to ~1e-9.
DEG_LON_KM = 2 * pi * EARTH_RADIUS_KM / 360.0 * cos(radians(1.2864))


def _point(km: float) -> tuple[Decimal, Decimal]:
    """A deterministic point `km` EAST of the stage along constant latitude."""
    dlon = Decimal(km / DEG_LON_KM).quantize(Decimal("0.000001"))
    return STAGE_LAT, STAGE_LON + dlon


# The pings 27's boundary suppression will kill (anchors + 1 km rings) shape
# which ping is "last visible": with every spacing > 1 km, exactly the two
# ANCHOR pings are suppressed, so the LAST VISIBLE ping is offsets[-2].
ETA_OFFSETS = [14.3, 13.0, 11.7, 10.4]  # last visible @11.7 km; implied 78 km/h
ETA_EXPECTED = (7.2, 12.0)  # 11.7 km / 78 km/h = 9 min base, ±25 % band
# ALERT fixture: last visible @1.9 km (INSIDE the 2 km default) with two
# visible fixes → implied 48 km/h → 1.9 km = 2.375 min base → (1.9, 3.1667).
ALERT_OFFSETS = [4.3, 2.7, 1.9, 0.7]
ALERT_EXPECTED = (1.9, 19.0 * 600 / 2400)  # lo exact 1.9; hi = 2.375/0.75 = 3.16


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world(db, fresh_user):
    """One rider, one route with stages (one WITH coords, one without),
    two ACTIVE vehicles on the route, a SACCO admin to authorize sessions."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_t31", "staff31@example.com", "***")
    sacco = Sacco.objects.create(name="Matata United T31")
    admin = User.objects.create_user("op_t31", "op31@example.com", "***")
    fleet_services.grant_sacco_admin(user=admin, sacco=sacco, granter=staff, now=T0)
    route = Route.objects.create(name_en="CBD–Kikuyu")
    stage_a = Stage.objects.create(name="Ambassadeur", lat=STAGE_LAT, lon=STAGE_LON)
    stage_b = Stage.objects.create(name="Gikomba")  # NO coordinates by design
    other_route = Route.objects.create(name_en="CBD–Dandora")
    RouteStage.objects.create(route=route, stage=stage_a, position=1)
    RouteStage.objects.create(route=route, stage=stage_b, position=2)
    veh = fleet_services.register_vehicle(
        sacco=sacco, raw_plate="KCB 220T", capacity=33, nickname="Mwangaza",
        routes=[route], actor=admin, now=T0,
    )
    other_veh = fleet_services.register_vehicle(
        sacco=sacco, raw_plate="KBX 456T", capacity=14, nickname="Machozi",
        routes=[route], actor=admin, now=T0,
    )
    return {
        "staff": staff, "sacco": sacco, "admin": admin,
        "rider": fresh_user,
        "route": route, "stage": stage_a, "stage_b": stage_b,
        "other_route": other_route, "veh": veh, "other_veh": other_veh,
    }


def _start_live(world, vehicle=None, *, now=T0):
    """A LIVE tracking session authorized through the SACCO-admin door."""
    session, _token = tracking_services.start_session(
        vehicle or world["veh"], by=world["admin"], cap_hours=12, now=now
    )
    return session


def _make_pings(session, offsets_km, *, end):
    """Pings at km-east offsets, one minute apart, ENDING at `end`.

    Spacing > 1 km means ONLY the two anchors get boundary-suppressed —
    so the second-to-last offset is provably the LAST VISIBLE ping.
    """
    for i, km in enumerate(offsets_km):
        at = end - timedelta(seconds=60 * (len(offsets_km) - 1 - i))
        lat, lon = _point(km)
        PingPoint.objects.create(
            session=session, lat=lat, lon=lon, recorded_at=at,
            received_at=at + timedelta(seconds=7),
        )


def _approach_session(world, *, probe_km, now=T0, vehicle=None):
    """Live session whose LAST VISIBLE ping sits `probe_km` east of the
    stage: three pings 2 km apart leave exactly the probe visible."""
    session = _start_live(world, vehicle, now=now)
    _make_pings(session, [probe_km + 2.0, probe_km, probe_km - 2.0], end=now - timedelta(minutes=1))
    return session


def _follow_vehicle(world, *, radius=None, stage=True, enabled=True):
    """An opted-in vehicle follow with (by default) the alert stage set."""
    follow = services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    if stage:
        services.set_alert_stage(follow, world["stage"])
    if radius is not None:
        services.set_radius(follow, radius)
    if enabled:
        services.toggle_notify(follow)
    follow.refresh_from_db()
    return follow


def _follow_route(world, *, enabled=True):
    """"An opted-in route follow (spec: route follows REQUIRE a stage)."""
    follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(follow, world["stage"])
    if enabled:
        services.toggle_notify(follow)
    follow.refresh_from_db()
    return follow


# --------------------------------------------------------- rolling speed
def test_rolling_speed_pure_mean_of_implied_speeds():
    """2 km between consecutive fixes over 2 minutes → exactly ~60 km/h."""
    p1 = SimpleNamespace(lat=_point(0.0)[0], lon=_point(0.0)[1],
                         recorded_at=T0 - timedelta(minutes=2), pk=1)
    p2 = SimpleNamespace(lat=_point(2.0)[0], lon=_point(2.0)[1],
                         recorded_at=T0, pk=2)
    speed = eta.rolling_speed([p1, p2], now=T0, window_min=10)
    assert speed == pytest.approx(60.0, abs=1e-3)


def test_rolling_speed_insufficient_and_stale_silence():
    """< 2 fixes, out-of-window fixes or zero-width segments → None, always."""
    one = SimpleNamespace(lat=_point(0.0)[0], lon=_point(0.0)[1],
                          recorded_at=T0, pk=1)
    assert eta.rolling_speed([one], now=T0, window_min=10) is None
    stale = SimpleNamespace(lat=_point(0.0)[0], lon=_point(0.0)[1],
                            recorded_at=T0 - timedelta(minutes=11), pk=2)
    second = SimpleNamespace(lat=_point(1.0)[0], lon=_point(1.0)[1],
                             recorded_at=T0 - timedelta(minutes=9), pk=3)
    # ONE ping inside the window is still insufficient (the older one is out).
    assert eta.rolling_speed([stale, second], now=T0, window_min=10) is None
    same_time = SimpleNamespace(lat=_point(2.0)[0], lon=_point(2.0)[1],
                                recorded_at=T0, pk=4)
    # Zero-width segment (duplicate moment): unusable, honest None.
    assert eta.rolling_speed([one, same_time], now=T0, window_min=10) is None


# ------------------------------------------------------------- pure bounds
def test_eta_minutes_from_exact_bounds():
    """THE exact pinned arithmetic: 10 km at 40 km/h → 15 min → (12, 20)."""
    assert eta.eta_minutes_from(10.0, 40.0) == (12.0, 20.0)
    # Guards: unusable speed never divides (the ∞ trap); missing → None.
    assert eta.eta_minutes_from(10.0, None) is None
    assert eta.eta_minutes_from(10.0, 0.0) is None
    assert eta.eta_minutes_from(None, 40.0) is None
    # A real zero distance is DATA ("any minute now"), never a missing 0.
    assert eta.eta_minutes_from(0.5, 40.0) == (0.6, 1.0)


def test_estimate_payload_label_is_part_of_the_shape():
    """The ESTIMATE label travels INSIDE the payload — key set pinned."""
    payload = eta.estimate_payload(7.2, 12.0)
    assert set(payload.keys()) == {"lo_min", "hi_min", "confidence"}
    assert payload["confidence"] == "ESTIMATE"  # Confidence.ESTIMATE.value
    assert eta.format_range((7.2, 12.0)) == "7–12 min"


# ------------------------------------------------------- eta through the door
def test_eta_range_computation_from_fixture_pings(world):
    """Fixture pings at a true 78 km/h roll: 11.7 km left → "7–12 min"."""
    session = _start_live(world)
    _make_pings(session, ETA_OFFSETS, end=T0 - timedelta(minutes=1))
    result = eta.eta_minutes(session, world["stage"], now=T0)
    assert result is not None
    lo, hi = result
    assert lo == pytest.approx(ETA_EXPECTED[0], rel=1e-3)
    assert hi == pytest.approx(ETA_EXPECTED[1], rel=1e-3)
    payload = eta.estimate_payload(*result)
    assert payload["confidence"] == "ESTIMATE"
    assert eta.format_range(result) == "7–12 min"


def test_eta_insufficient_data_silence_not_zero_not_infinity(world):
    """A live session whose visible window is empty → full silence."""
    session = _start_live(world)
    # Two pings 1 km apart: anchors + ring suppress EVERYTHING → 0 visible.
    _make_pings(session, [1.0, 0.0], end=T0 - timedelta(minutes=1))
    assert eta.eta_minutes(session, world["stage"], now=T0) is None
    route_follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(route_follow, world["stage"])
    data = services.stage_arrivals(
        world["route"], world["stage"], user=world["rider"], now=T0
    )
    assert data["rows"], "route follow keeps the row on the page"
    row = data["rows"][0]
    assert row["live"] is True  # it IS publishing — the data is just thin
    assert row["estimate"] is None          # NOT 0, NOT a fake range
    assert row["estimate_text"] is None
    assert str(row["estimate_status"]) == str(eta.MSG_INSUFFICIENT_DATA)
    assert row["approaching"] is False  # unknown distance, no trigger


def test_eta_not_tracking_silence(world):
    """No live session → 'Not tracking right now.', no numbers fabricated."""
    route_follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(route_follow, world["stage"])
    data = services.stage_arrivals(
        world["route"], world["stage"], user=world["rider"], now=T0
    )
    row = data["rows"][0]
    assert row["live"] is False
    assert row["estimate"] is None and row["distance_km"] is None
    assert str(row["estimate_status"]) == str(eta.MSG_NOT_TRACKING)


# ------------------------------------------------------- the radius trigger
def test_approaching_inside_radius_true(world):
    """Last visible ping 1.5 km out → inside the 2 km default radius."""
    session = _approach_session(world, probe_km=1.5)
    stage = world["stage"]
    last = eta.last_visible_ping(session, now=T0)
    assert distance_km((float(last.lat), float(last.lon)), stage.coordinate) == pytest.approx(1.5, abs=1e-3)
    assert eta.approaching_stage(session, stage, now=T0) is True


def test_approaching_outside_radius_false(world):
    """2.5 km out → beyond the 2 km default: no trigger (no early lie)."""
    session = _approach_session(world, probe_km=2.5)
    assert eta.approaching_stage(session, world["stage"], now=T0) is False


def test_approaching_boundary_is_inclusive(world):
    """The radius boundary fires AT the radius (<= semantics, like 19/27)."""
    session = _approach_session(world, probe_km=2.0)
    stage = world["stage"]
    last = eta.last_visible_ping(session, now=T0)
    dist = distance_km((float(last.lat), float(last.lon)), stage.coordinate)
    assert dist == pytest.approx(2.0, abs=1e-3)
    assert eta.approaching_stage(session, stage, radius_km=dist, now=T0) is True
    assert eta.approaching_stage(
        session, stage, radius_km=dist - 0.0001, now=T0
    ) is False


def test_approaching_stage_without_coordinates_is_honest_false(world):
    """A stage with unknown coordinates can never trigger — no fabrication."""
    session = _approach_session(world, probe_km=1.0)
    assert world["stage_b"].coordinate is None
    assert eta.approaching_stage(session, world["stage_b"], now=T0) is False


# ---------------------------------------------------------------- CRUD
def test_follow_vehicle_by_nickname_icase_and_duplicate_refused(world):
    """Follow by NICKNAME (icase-first matching); duplicate refused."""
    follow = services.follow_vehicle(user=world["rider"], nickname="mwangaza")
    assert follow.vehicle.pk == world["veh"].pk
    assert follow.notify_enabled is False  # opt-in starts OFF
    assert follow.radius_km == DEFAULT_RADIUS_KM == 2
    # Duplicate: refused (unique pair — the service, not an IntegrityError).
    with pytest.raises(ValidationError):
        services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    assert VehicleFollow.objects.filter(user=world["rider"]).count() == 1
    # The DB constraint is the backstop a rogue writer cannot bypass.
    with pytest.raises(Exception):
        VehicleFollow.objects.create(user=world["rider"], vehicle=world["veh"])


def test_follow_vehicle_ambiguous_nickname_refused(world):
    """Two ACTIVE matatus share the nickname → plain refusal, no guesses."""
    User = get_user_model()
    staff2 = User.objects.create_superuser("staff_x2", "staffx2@example.com", "***")
    sacco2 = Sacco.objects.create(name="Other Operators T31")
    admin2 = User.objects.create_user("op_x2", "opx2@example.com", "***")
    fleet_services.grant_sacco_admin(user=admin2, sacco=sacco2, granter=staff2, now=T0)
    fleet_services.register_vehicle(
        sacco=sacco2, raw_plate="KCX 111T", capacity=14, nickname="Twende",
        actor=admin2, now=T0,
    )
    fleet_services.register_vehicle(
        sacco=sacco2, raw_plate="KCX 112T", capacity=14, nickname="Twende",
        actor=admin2, now=T0,
    )
    with pytest.raises(services.AmbiguousNickname) as exc:
        services.follow_vehicle(user=world["rider"], nickname="Twende")
    assert "more than one" in str(exc.value)  # plain, actionable rider words


def test_follow_vehicle_exact_beats_contains_and_retired_unresolvable(world):
    """"Mwangaza" resolves EXACTLY even when "Mwangaza Mpya" also exists;
    retired metal never resolves (nickname of only-retired → not found)."""
    fleet_services.register_vehicle(
        sacco=world["sacco"], raw_plate="KCZ 999T", capacity=51,
        nickname="Mwangaza Mpya", actor=world["admin"], now=T0,
    )
    resolved = services.resolve_vehicle_by_nickname("mwangaza")
    assert resolved.pk == world["veh"].pk
    # Retire the twin so only RETIRED metal holds "Machozi" → honest refusal.
    fleet_services.retire_vehicle(world["other_veh"], actor=world["admin"], now=T0)
    with pytest.raises(services.NicknameNotFound):
        services.resolve_vehicle_by_nickname("Machozi")
    with pytest.raises(services.NicknameNotFound):
        services.resolve_vehicle_by_nickname("Nonexistent")


def test_follow_route_crud_radius_bounds_and_unfollow(world):
    """Route CRUD + radius bounds (0/11/string refused; 5 lands)."""
    follow = services.follow_route(user=world["rider"], route=world["route"])
    assert follow.notify_enabled is False
    with pytest.raises(ValidationError):
        services.follow_route(user=world["rider"], route=world["route"])
    services.set_radius(follow, 5)
    follow.refresh_from_db()
    assert follow.radius_km == 5
    for bad in (0, 11, "5", -2):
        with pytest.raises(ValidationError):
            services.set_radius(follow, bad)
    assert services.unfollow_route(user=world["rider"], route=world["route"]) is True
    assert services.unfollow_route(user=world["rider"], route=world["route"]) is False  # idempotent
    assert services.unfollow_vehicle(user=world["rider"], vehicle=world["veh"]) is False


def test_set_alert_stage_must_lie_on_the_followed_path(world):
    """Route follow: stage off-route refused; on-route ok. Vehicle follow:
    stage must belong to a route the vehicle operates."""
    fol = services.follow_route(user=world["rider"], route=world["route"])
    far_stage = Stage.objects.create(name="Mlolongo", lat=Decimal("-1.4"), lon=Decimal("36.9"))
    with pytest.raises(ValidationError) as refused:
        services.set_alert_stage(fol, far_stage)  # NOT on CBD–Kikuyu
    assert "stage" in str(refused.value).lower()  # plain refusal text
    services.set_alert_stage(fol, world["stage"])
    fol.refresh_from_db()
    assert fol.notify_stage_id == world["stage"].pk
    services.set_alert_stage(fol, None)  # clear → honest no-trigger state
    assert fol.notify_stage_id is None
    # vehicle follow: the stage must be reachable by THAT vehicle
    vf = _follow_vehicle(world, enabled=False)
    with pytest.raises(ValidationError):
        services.set_alert_stage(vf, far_stage)
    services.set_alert_stage(vf, world["stage"])
    assert vf.notify_stage_id == world["stage"].pk


# ------------------------------------------------------- opt-in + scans
def test_opt_out_default_sends_nothing(world):
    """DEFAULT OFF: even a perfectly approaching matatu sends NOTHING until
    the rider taps notify on — for BOTH follow kinds."""
    _approach_session(world, probe_km=1.0)
    services.set_alert_stage(_follow_vehicle(world, enabled=False), world["stage"])
    route_follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(route_follow, world["stage"])
    mock = notify.MockPush()
    assert services.alert_scan(now=T0, adapter=mock) == []
    assert mock.sent == []
    assert FollowAlert.objects.count() == 0


def test_route_follow_alert_fires_with_estimate_label(world):
    """"My matatu is coming": enable → the scan sends a deterministic push
    carrying the estimate RANGE inside its body, receipt on the ledger."""
    route_follow = _follow_route(world)  # stage set, notify ON
    session = _start_live(world)
    _make_pings(session, ALERT_OFFSETS, end=T0 - timedelta(minutes=1))
    mock = notify.MockPush()
    results = services.check_and_notify([route_follow], now=T0, adapter=mock)
    assert len(results) == 1
    row = results[0]
    assert row["follow_kind"] == "ROUTE"
    assert row["vehicle_id"] == world["veh"].pk
    assert row["stage_id"] == world["stage"].pk
    assert row["adapter_id"] == "mock:1"
    assert row["estimate"]["confidence"] == "ESTIMATE"  # label pinned
    assert row["estimate"]["lo_min"] == pytest.approx(ALERT_EXPECTED[0], rel=1e-3)
    assert "2–3 min" in row["body"]  # honest range in the push text
    ledger = FollowAlert.objects.get(session=session)
    assert ledger.adapter_id == "mock:1"
    assert ledger.follow_kind == "ROUTE"
    assert "Ambassadeur" in ledger.title


def test_dedupe_window_one_notification_per_approach_event(world):
    """+10 min after the first announce → still ONE send. +35 min (past the
    30-min window) → the SAME approaching event may announce again."""
    route_follow = _follow_route(world)
    session = _start_live(world)
    _make_pings(session, [3.5, 1.5, -0.5], end=T0 - timedelta(minutes=1))
    first = services.alert_scan(now=T0, adapter=notify.MockPush())
    assert len(first) == 1
    assert first[0]["estimate"] is None  # single visible fix → push without ETA
    # +10 min — inside the 30-min window → suppressed (one alert per event).
    assert services.alert_scan(now=T0 + timedelta(minutes=10), adapter=notify.MockPush()) == []
    # +35 min — window closed; approach still true → the trigger may repeat.
    second = services.alert_scan(now=T0 + timedelta(minutes=35), adapter=notify.MockPush())
    assert len(second) == 1
    assert FollowAlert.objects.count() == 2


def test_mock_push_determinism_and_registry(world):
    """Two identical scans over identical state → IDENTICAL recorded sends;
    receipts are mock:1-style ids; the registry refuses unknown names."""
    route_follow = _follow_route(world)
    session = _start_live(world)
    _make_pings(session, [3.5, 1.5, -0.5], end=T0 - timedelta(minutes=1))
    mock_a = notify.MockPush()
    result_a = services.alert_scan(now=T0, adapter=mock_a)
    # Reset ONLY the ledger (same pre-state, same clock) and run again.
    FollowAlert.objects.all().delete()
    mock_b = notify.MockPush()
    result_b = services.alert_scan(now=T0, adapter=mock_b)
    assert mock_a.sent == mock_b.sent  # determinism contract
    assert result_a == result_b        # scan output just as deterministic
    assert mock_a.sent[0]["body"] == mock_b.sent[0]["body"]
    # Registry: default mock; webpush stub is loud, never networked.
    assert isinstance(notify.get_adapter(), notify.MockPush)
    stub = notify.get_adapter("webpush")
    assert stub.is_stub is True
    with pytest.raises(NotImplementedError):
        stub.send(route_follow, "t", "b")
    with pytest.raises(ValueError):
        notify.get_adapter("carrier-pigeon")


def test_vehicle_follow_alert_honours_its_own_radius(world):
    """A vehicle follow alerts on ITS radius: 2.1 km vs radius 2 → nothing;
    radius 3 → the send (per-follow configurable radius, both kinds)."""
    follow = _follow_vehicle(world, radius=2)
    session = _approach_session(world, probe_km=2.1)  # just outside 2 km
    assert services.check_and_notify([follow], now=T0, adapter=notify.MockPush()) == []
    services.set_radius(follow, 3)
    sent = services.check_and_notify([follow], now=T0, adapter=notify.MockPush())
    assert len(sent) == 1
    assert sent[0]["follow_kind"] == "VEHICLE"
    assert sent[0]["estimate"] is None  # 1 visible fix → honest push, no ETA


def test_alert_scan_skips_follows_without_stage(world):
    """A follow with notify on but NO stage chosen raises NO alert —
    honest: the rider never picked a trigger."""
    follow = services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    services.toggle_notify(follow)  # ON, but no stage yet
    _approach_session(world, probe_km=0.5)
    assert services.alert_scan(now=T0, adapter=notify.MockPush()) == []
    assert FollowAlert.objects.count() == 0


# ------------------------------------------------------------------ views
def test_follows_page_login_gate_and_content(world, client):
    """Anonymous → redirected to login; the rider sees her own rows."""
    url = reverse("alerts:follows")
    anon = client.get(url)
    assert anon.status_code == 302
    client.force_login(world["rider"])
    response = client.get(url)
    assert response.status_code == 200
    body = response.content.decode()
    assert 'data-test="alerts-follow-row"' not in body  # nothing followed yet
    services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    body = client.get(url).content.decode()
    assert 'data-test="alerts-follow-row"' in body and "KCB-220-T" in body
    assert "{% trans" not in body  # i18n markers live in source, not output
    assert_under_budget(body, kb=50)


def test_follow_crud_views_one_tap_flow(world, client_auth):
    """add (302 + row) → toggle (OFF→ON) → stage → radius → remove."""
    client = client_auth
    base = reverse("alerts:follows")
    # ADD by nickname
    response = client.post(reverse("alerts:follow_vehicle"), {"nickname": "Mwangaza"})
    assert response.status_code == 302
    follow = VehicleFollow.objects.get(user=world["rider"])
    assert follow.notify_enabled is False
    # DUPLICATE → refused with the plain message
    response = client.post(reverse("alerts:follow_vehicle"), {"nickname": "Mwangaza"})
    assert VehicleFollow.objects.count() == 1
    # TOGGLE — the one-tap consent switch
    client.post(reverse("alerts:toggle_notify", args=["vehicle", follow.pk]), {})
    assert VehicleFollow.objects.get(pk=follow.pk).notify_enabled is True
    client.post(reverse("alerts:toggle_notify", args=["vehicle", follow.pk]), {})
    assert VehicleFollow.objects.get(pk=follow.pk).notify_enabled is False
    # STAGE — on-path accepted
    client.post(
        reverse("alerts:set_stage", args=["vehicle", follow.pk]),
        {"stage_id": world["stage"].pk},
    )
    assert VehicleFollow.objects.get(pk=follow.pk).notify_stage_id == world["stage"].pk
    # RADIUS — bounds enforced at the view boundary too
    client.post(reverse("alerts:set_radius", args=["vehicle", follow.pk]), {"radius_km": "7"})
    assert VehicleFollow.objects.get(pk=follow.pk).radius_km == 7
    client.post(reverse("alerts:set_radius", args=["vehicle", follow.pk]), {"radius_km": "42"})
    assert VehicleFollow.objects.get(pk=follow.pk).radius_km == 7
    # REMOVE
    client.post(reverse("alerts:remove_follow", args=["vehicle", follow.pk]), {})
    assert not VehicleFollow.objects.filter(pk=follow.pk).exists()
    assert client.get(base).status_code == 200


def test_set_stage_refuses_off_path_stage(world, client_auth):
    """A stage NOT on the followed path is refused with the plain message
    and the previous choice survives."""
    follow = _follow_vehicle(world, enabled=False)
    stage_far = Stage.objects.create(name="Mlolongo", lat=Decimal("-1.4"), lon=Decimal("36.9"))
    client_auth.post(
        reverse("alerts:set_stage", args=["vehicle", follow.pk]),
        {"stage_id": stage_far.pk},
    )
    follow.refresh_from_db()
    assert follow.notify_stage_id == world["stage"].pk  # unchanged by refusal


def test_route_follow_view_flow(world, client_auth):
    """Route follows add through their own door + appear on the page."""
    response = client_auth.post(
        reverse("alerts:follow_route"), {"route_id": world["route"].pk}
    )
    assert response.status_code == 302
    assert RouteFollow.objects.filter(user=world["rider"]).exists()
    body = client_auth.get(reverse("alerts:follows")).content.decode()
    assert "CBD–Kikuyu" in body


def test_arrivals_page_estimate_and_honest_silence_together(world, client_auth):
    """THE acceptance surface: one followed matatu ESTIMATED (7–12 min with
    its ESTIMATE badge), one followed matatu honestly silent — one page."""
    client = client_auth
    session = _start_live(world, now=VNOW)
    _make_pings(session, ETA_OFFSETS, end=VNOW - timedelta(minutes=1))
    # other_veh (on the route) has NO session → the honest silence row
    services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    route_follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(route_follow, world["stage"])
    url = reverse(
        "alerts:arrivals",
        kwargs={"route_id": world["route"].pk, "stage_id": world["stage"].pk},
    )
    body = client.get(url).content.decode()
    assert "7–12 min" in body          # the visible RANGE
    assert "ESTIMATE" in body          # the label, always attached
    assert "Not tracking right now" in body  # the honest silence
    assert_under_budget(body, kb=50)
    # Stranger sees nothing (rows are the RIDER's own follows).
    rider2 = get_user_model().objects.create_user("other_t31b", "o31b@example.com", "***")
    client.force_login(rider2)
    body = client.get(url).content.decode()
    assert "Mwangaza" not in body


def test_stage_arrivals_json_mirror(world, client_auth):
    """"The JSON status mirror: same rows, same labels, machine-readable;
    a missing estimate is null — never 0 or Infinity anywhere."""
    client = client_auth
    session = _start_live(world, now=VNOW)
    _make_pings(session, ETA_OFFSETS, end=VNOW - timedelta(minutes=1))
    services.follow_vehicle(user=world["rider"], nickname="Mwangaza")
    route_follow = services.follow_route(user=world["rider"], route=world["route"])
    services.set_alert_stage(route_follow, world["stage"])
    url = reverse(
        "alerts:status",
        kwargs={"route_id": world["route"].pk, "stage_id": world["stage"].pk},
    )
    response = client.get(url)
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload["route_id"] == world["route"].pk
    assert payload["stage_id"] == world["stage"].pk
    assert set(payload.keys()) == {"route_id", "stage_id", "stage", "rows", "generated_at"}
    raw = response.content.decode()
    assert "Infinity" not in raw and '"NaN"' not in raw
    live_rows = [r for r in payload["rows"] if r["live"]]
    assert live_rows, "the tracking vehicle's row exists"
    estimate = live_rows[0]["estimate"]
    assert set(estimate.keys()) == {"lo_min", "hi_min", "confidence"}
    assert estimate["confidence"] == "ESTIMATE"
    assert estimate["lo_min"] > 0.0 and estimate["hi_min"] > 0.0  # never 0/∞
    silent_rows = [r for r in payload["rows"] if not r["live"]]
    assert silent_rows[0]["estimate"] is None
    assert "Not tracking" in silent_rows[0]["estimate_status"]


def test_stage_arrivals_union_dedupes_vehicle_follow_and_route_follow(world):
    """Following BOTH the route and one of its vehicles keeps ONE row per
    vehicle; the vehicle follow's own radius wins over the route's."""
    services.follow_vehicle(user=world["rider"], nickname="Mwangaza")  # radius 2
    rf = services.follow_route(user=world["rider"], route=world["route"])
    services.set_radius(rf, 3)
    _approach_session(world, probe_km=2.5)  # outside 2 (vehicle) inside 3 (route)
    data = services.stage_arrivals(
        world["route"], world["stage"], user=world["rider"], now=T0
    )
    assert len(data["rows"]) == 2  # one row PER VEHICLE (deduped by pk)
    by_plate = {row["plate"]: row for row in data["rows"]}
    assert by_plate["KCB-220-T"]["approaching"] is False  # veh radius 2 wins
    assert by_plate["KBX-456-T"]["live"] is False  # no session → silence
    # Drop the vehicle follow → the route follow's radius 3 now applies.
    services.unfollow_vehicle(user=world["rider"], vehicle=world["veh"])
    data = services.stage_arrivals(
        world["route"], world["stage"], user=world["rider"], now=T0
    )
    rows = {row["plate"]: row for row in data["rows"]}
    assert rows["KCB-220-T"]["approaching"] is True  # route radius 3 catches it
