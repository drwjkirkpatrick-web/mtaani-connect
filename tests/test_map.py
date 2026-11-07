"""tests/test_map.py — Prompt 28: the live map (Leaflet + OSM, honest state).

Coverage (T0 = injected clock for the pure serializer; VNOW = the real clock
the VIEWS inject; ONLY the 27/30 public doors feed every payload):

  * map:positions — exact top-level shape (route_id, route_name, sessions,
    crowd, empty, as_of); session entries EXACTLY 27's key set; positions
    carry the Prompt 28 confidence COLUMN (a downgraded ping leaves the door
    labeled ESTIMATE — visible, never silently upgraded);
  * the crowd overlay rides per 30's frozen contract: entries dict-equal to
    crowd_overlay_for_stage's DIRECT output under the same scorer, NO position
    keys on any entry, pins anchored at STAGE geometry only;
  * the scorer is injectable (0.0 vs 1.0 reweights; clock stays injected);
  * route isolation: ?route=A carries A's sessions only; no-param = network;
    unknown route → 404 (never a substituted map);
  * EMPTY STATE: the explicit notice + a data island with ZERO markers —
    never a placeholder marker or spun-up vehicle;
  * tile pitfalls pinned: maxNativeZoom/maxZoom caps, updateWhenIdle lazy
    flag, integrity attrs ×2, defer, preconnects, no Google, the 2 MiB
    TILE_BUDGET_BYTES + tile_budget_note stub;
  * marker CSS vocabulary per Confidence (filled/hollow/dashed-ring);
  * page ≤ 50 KB; i18n marks; no crew identity anywhere.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.core.testing import assert_under_budget, mock_now
from apps.crowd import serialization as crowd_serialization
from apps.crowd import services as crowd_services
from apps.fleet import services as fleet_services
from apps.map import serialization as map_serialization
from apps.map import tiling
from apps.routes.models import Route, RouteStage, Sacco, Stage
from apps.tracking import ingest
from apps.tracking import services as tracking_services
from apps.tracking.models import PingPoint

T0 = mock_now()   # injected clock — pure serializer tests name now= always
VNOW = dj_timezone.now()  # the clock the map VIEWS inject

KMB_PER_DEG_LON = 111.32 * 0.999748  # km per degree of lon at the CBD latitude


def _east(base_lon: Decimal, km: float) -> Decimal:
    return base_lon + Decimal(km / KMB_PER_DEG_LON).quantize(Decimal("0.000001"))


# ----------------------------------------------------------------- fixtures
@pytest.fixture
def world(db):
    """Route network (two routes, one coordinate-less stage), one SACCO with
    an ADMIN, an APPROVED crew member (identity-bait) on vehicle A."""
    User = get_user_model()
    staff = User.objects.create_superuser("staff_t28map", "staff@example.com", "***")
    sacco = Sacco.objects.create(name="Map SACCO")
    admin = User.objects.create_user("op_t28map", "op@example.com", "***")
    crew = User.objects.create_user("driver_bait_t28", "crew@example.com", "***")
    crew.phone = "+254****0777"  # BAIT: must never sit in any map surface
    crew.save()
    fleet_services.grant_sacco_admin(user=admin, sacco=sacco, granter=staff, now=T0)

    route_a = Route.objects.create(name_en="Ambassadeur - Kinoo")
    route_b = Route.objects.create(name_en="CBD - Ruiru")
    s_amb = Stage.objects.create(
        name="Ambassadeur", lat=Decimal("-1.286400"), lon=Decimal("36.817200")
    )
    s_kin = Stage.objects.create(
        name="Kinoo", lat=Decimal("-1.292200"), lon=Decimal("36.731700")
    )
    s_unmapped = Stage.objects.create(name="Kilometre 47")  # no coordinates
    RouteStage.objects.create(route=route_a, stage=s_amb, position=1)
    RouteStage.objects.create(route=route_a, stage=s_kin, position=2)
    RouteStage.objects.create(route=route_a, stage=s_unmapped, position=3)

    veh_a = fleet_services.register_vehicle(
        sacco=sacco, raw_plate="KCB 220P", capacity=33,
        nickname="Mwangaza", routes=[route_a], actor=admin, now=T0,
    )
    veh_b = fleet_services.register_vehicle(
        sacco=sacco, raw_plate="KBX 456M", capacity=14,
        nickname="Machozi", routes=[route_b], actor=admin, now=T0,
    )
    seat_a = fleet_services.request_crew(
        user=crew, vehicle=veh_a, role="DRIVER", now=T0
    )
    fleet_services.approve_crew(seat_a, approver=admin, now=T0)
    return {
        "staff": staff, "sacco": sacco, "admin": admin, "crew": crew,
        "route_a": route_a, "route_b": route_b,
        "s_amb": s_amb, "s_kin": s_kin, "s_unmapped": s_unmapped,
        "veh_a": veh_a, "veh_b": veh_b,
    }


def _ping(base, minutes: float, east_km: float, speed=None) -> dict:
    item = {
        "recorded_at": (base + timedelta(minutes=minutes)).isoformat(),
        "lat": -1.286400,
        "lon": float(_east(Decimal("36.817200"), east_km)),
    }
    if speed is not None:
        item["speed_kmh"] = speed
    return item


def _publish(published_session, token, base, *, at=None):
    """Round-trip through the REAL ingest (batch→map acceptance): anchor,
    a LIVE middle fix, an impossible jump → ESTIMATE, a suppressed tail."""
    ingest.ingest_pings(
        token, published_session.pk, published_session.vehicle_id,
        [
            _ping(base, 1, 0.0),        # start anchor (suppressed publicly)
            _ping(base, 3, 2.0, 55.0),  # LIVE_GPS, visible
            _ping(base, 6, 20.0),       # ~360 km/h jump → ESTIMATE, visible
            _ping(base, 7, 25.0),       # end anchor (suppressed publicly; the
                                        # 20.0 fix is >1 km inside 27's ring)
        ],
        now=at or (base + timedelta(minutes=8)),
    )


def _crowd_file(world, base):
    """Two merged DELAY riders at Ambassadeur + FLOODING at Kinoo."""
    User = get_user_model()
    rider_one = User.objects.create_user("rider_one", "r1@example.com", "***")
    rider_two = User.objects.create_user("rider_two", "r2@example.com", "***")
    crowd_services.file_report(
        reporter=rider_one, stage=world["s_amb"], kind="DELAY",
        route=world["route_a"], note="Kanjo everywhere",
        now=base + timedelta(minutes=2),
    )
    _, outcome = crowd_services.file_report(
        reporter=rider_two, stage=world["s_amb"], kind="DELAY",
        route=world["route_a"], now=base + timedelta(minutes=3),
    )
    assert outcome.value == "MERGED"  # the fixture itself proves the merge
    crowd_services.file_report(
        reporter=rider_one, stage=world["s_kin"], kind="FLOODING",
        route=world["route_a"], now=base + timedelta(minutes=4),
    )


# -- pure-serializer world (T0): services under the injected clock ----------
@pytest.fixture
def crowded(world):
    _crowd_file(world, T0)


@pytest.fixture
def live_on_route_a(world):
    session, raw_token = tracking_services.start_session(
        world["veh_a"], by=world["crew"], now=T0
    )
    _publish(session, raw_token, base=T0)
    return {"session": session, "raw_token": raw_token}


# -- view world (VNOW): the views inject timezone.now() — everything that a
#    map VIEW must see live/crowded is created at the REAL clock ----------
@pytest.fixture
def publishing_now(world):
    session, raw_token = tracking_services.start_session(
        world["veh_a"], by=world["crew"]  # real clock → live on the page
    )
    _publish(session, raw_token, base=VNOW)
    return {"session": session, "raw_token": raw_token}


@pytest.fixture
def crowded_now(world):
    _crowd_file(world, VNOW)


def _positions_json(client, route=None) -> dict:
    url = reverse("map:positions")
    if route is not None:
        url = f"{url}?route={route.pk}"
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    return json.loads(response.content)


def _data_island(body: str) -> dict:
    """The page's ONE payload island (json_script) as JSON — the FIRST
    script close after the marker closes the island (the page's own init
    script sits AFTER it)."""
    marker = 'id="map-data" type="application/json">'
    return json.loads(body.split(marker)[1].split("</script>", 1)[0])


# ============================================================== payload shape
def test_positions_payload_has_the_exact_documented_shape(
    client, world, publishing_now
):
    payload = _positions_json(client, world["route_a"])
    assert set(payload) == {
        "route_id", "route_name", "sessions", "crowd", "empty", "as_of",
    }
    assert payload["route_id"] == world["route_a"].pk
    assert payload["route_name"] == "Ambassadeur - Kinoo"
    assert payload["empty"] is False
    (entry,) = payload["sessions"]  # route isolation: exactly veh_a's session
    assert set(entry) == {
        "session_id", "vehicle_id", "plate", "nickname", "live", "started_at",
        "positions",
    }  # EXACTLY 27's frozen session shape
    assert entry["plate"] == "KCB-220-P"
    assert entry["live"] is True
    # 4 fixes written; boundary suppression keeps exactly the middle two:
    assert len(entry["positions"]) == 2
    live_fix, estimate_fix = entry["positions"]
    assert set(live_fix) == {
        "session_id", "vehicle_id", "lat", "lon", "recorded_at", "confidence",
    }
    assert live_fix["confidence"] == "LIVE_GPS"
    assert estimate_fix["confidence"] == "ESTIMATE"  # the downgrade is VISIBLE
    assert estimate_fix["session_id"] == entry["session_id"]


def test_positions_without_route_carries_the_whose_network(
    client, world, publishing_now
):
    payload = _positions_json(client)  # citywide: no ?route=
    assert payload["route_id"] is None
    assert payload["route_name"] is None
    assert {entry["vehicle_id"] for entry in payload["sessions"]} == {
        world["veh_a"].pk
    }  # veh_b has no session — nothing drawn for it (honest silence)


def test_route_isolation_keeps_other_routes_out(client, world, publishing_now):
    """A second LIVE session (route B's vehicle) must NOT enter route A's
    payload — and the mirror holds B's too."""
    session_b, token_b = tracking_services.start_session(
        world["veh_b"], by=world["admin"], now=VNOW  # start at the read clock:
        # a future start_at is NOT live at the view's timezone.now() read
    )
    ingest.ingest_pings(
        token_b, session_b.pk, session_b.vehicle_id,
        [
            _ping(VNOW, 2, 0.0),
            _ping(VNOW, 4, 4.0, 70.0),   # visible live fix for B
            _ping(VNOW, 6, 8.0),         # end anchor
        ],
        now=VNOW + timedelta(minutes=8),
    )
    payload_a = _positions_json(client, world["route_a"])
    assert {e["vehicle_id"] for e in payload_a["sessions"]} == {world["veh_a"].pk}
    payload_b = _positions_json(client, world["route_b"])
    assert {e["vehicle_id"] for e in payload_b["sessions"]} == {session_b.vehicle_id}


def test_unknown_route_is_404_not_a_substituted_map(client, world, db):
    assert client.get(f"{reverse('map:positions')}?route=999999").status_code == 404
    assert client.get(f"{reverse('map:route')}?route=notanumber").status_code == 404


# ======================================================== crowd overlay seam
def test_crowd_entries_are_dict_EQUAL_to_30s_serializer_output(world, crowded, db):
    """THE strongest AS-IS pin: under the SAME scorer + injected now, the map
    payload's crowd entries EQUAL 30's serializer output directly — zero
    edits, zero drift, and any future key change shows up immediately."""
    scorer = crowd_services.make_report_scorer()
    read_at = T0 + timedelta(minutes=6)
    direct = []
    for link in world["route_a"].stage_order:
        direct.extend(
            crowd_serialization.crowd_overlay_for_stage(
                link.stage, now=read_at, scorer=scorer
            )
        )
    payload = map_serialization.route_positions_payload(
        world["route_a"], now=read_at, scorer=scorer
    )
    assert payload["crowd"]["entries"] == direct
    assert list(payload["crowd"]["entries"][0]) == list(direct[0])  # same keys
    delay_row, flooding_row = direct[0], direct[1]  # route stage order (pos 1, 2)
    assert delay_row["confidence"] == "COMMUNITY_REPORT"
    assert delay_row["count"] == 2
    assert delay_row["type_label"] == str(
        crowd_serialization.ReportType("DELAY").label
    )
    assert flooding_row["count"] == 1
    for row in payload["crowd"]["entries"]:  # NO position keys on ANY entry:
        for forbidden in ("lat", "lon", "position", "positions", "coordinates"):
            assert forbidden not in row, f"crowd entry carries {forbidden}"


def test_crowd_anchors_are_stage_geometry_only(
    client, world, crowded_now, publishing_now
):
    payload = _positions_json(client, world["route_a"])
    anchors = payload["crowd"]["anchors"]
    by_id = {a["stage_id"]: a for a in anchors}
    assert set(by_id) == {world["s_amb"].pk, world["s_kin"].pk}  # ONLY mapped
    assert by_id[world["s_amb"].pk]["lat"] == pytest.approx(-1.2864, abs=1e-6)
    assert by_id[world["s_kin"].pk]["lon"] == pytest.approx(36.7317, abs=1e-6)
    assert world["s_unmapped"].pk not in by_id  # no coordinates → no pin


def test_crowd_scorer_is_injectable_and_it_moves_the_weights(world, crowded, db):
    """30's contract, map side: scorer in → weights move; clock injected."""
    zero = map_serialization.route_positions_payload(
        world["route_a"], now=T0 + timedelta(minutes=6), scorer=lambda u: 0.0,
    )
    trusted = map_serialization.route_positions_payload(
        world["route_a"], now=T0 + timedelta(minutes=6), scorer=lambda u: 1.0,
    )
    zero_delay = next(r for r in zero["crowd"]["entries"] if r["type"] == "DELAY")
    trusted_delay = next(
        r for r in trusted["crowd"]["entries"] if r["type"] == "DELAY"
    )
    assert trusted_delay["weight"] > zero_delay["weight"]  # trust moved truth


def test_crowd_reports_never_fabricate_a_session(world, crowded, db):
    """Reports alone MUST NOT create any session/vehicle pseudo-marker."""
    payload = map_serialization.route_positions_payload(
        world["route_a"], now=T0 + timedelta(minutes=6), scorer=lambda u: 1.0,
    )
    assert payload["sessions"] == []
    assert payload["crowd"]["entries"]  # the reports ARE there,
    assert payload["empty"] is False    # but nothing invented a position


# ================================================================= empty state
def test_empty_state_is_the_explicit_notice_not_a_marker(client, world, db):
    """Route B: vehicle assigned, NO session, NO reports → the page says
    'no live data for this route' and the island carries ZERO markers."""
    body = client.get(
        f"{reverse('map:route')}?route={world['route_b'].pk}"
    ).content.decode()
    assert 'data-test="map-empty-state"' in body
    assert "No live data for this route" in body
    island = _data_island(body)
    assert island["sessions"] == []
    assert island["crowd"]["entries"] == [] and island["crowd"]["anchors"] == []
    assert island["empty"] is True


def test_empty_citywide_map_is_honest_too(client, world, db):
    body = client.get(reverse("map:route")).content.decode()
    assert 'data-test="map-empty-state"' in body
    assert "No live matatu data anywhere right now." in body
    assert 'data-test="map-legend"' in body
    assert _data_island(body)["empty"] is True


def test_ended_session_leaves_the_map_empty_again(client, world, publishing_now):
    """After the crew stops, the route is honestly empty — the past never
    leaks onto the public map (27's gate flows through the map's doors)."""
    tracking_services.end_session(
        publishing_now["session"], by=world["crew"],
        now=VNOW + timedelta(minutes=30),
    )
    body = client.get(
        f"{reverse('map:route')}?route={world['route_a'].pk}"
    ).content.decode()
    assert 'data-test="map-empty-state"' in body
    assert "No live data for this route" in body


# ========================================================= marker vocabulary
def test_marker_css_vocabulary_is_pinned(client, world, publishing_now, db):
    body = client.get(reverse("map:route")).content.decode()
    assert "LIVE_GPS" in body and "conf-live" in body           # filled dot
    assert "COMMUNITY_REPORT" in body and "conf-report" in body  # hollow dot
    assert "ESTIMATE" in body and "conf-estimate" in body        # dashed ring
    assert css_has(body, ".conf-live", "background: #1565c0")       # filled
    assert css_has(body, ".conf-report", "border: 2.5px solid")     # hollow
    assert css_has(body, ".conf-estimate", "border: 2.5px dashed")  # ring
    assert 'data-test="map-legend"' in body
    # …and the JS mapping keys the payload's confidences to those classes:
    assert "LIVE_GPS: \"conf-live\"" in body
    assert "COMMUNITY_REPORT: \"conf-report\"" in body
    assert "ESTIMATE: \"conf-estimate\"" in body


def css_has(body: str, selector: str, declaration: str) -> bool:
    """The declaration sits in the CSS rule carrying that selector."""
    for chunk in body.split(selector)[1:]:
        if declaration in chunk.split("}")[0]:
            return True
    return False


# ============================================================== tile pitfalls
def test_tile_pitfall_pins(client, world, db):
    body = client.get(reverse("map:route")).content.decode()
    assert '"maxNativeZoom"' in body           # fetch cap (the bytes cap)
    assert '"maxZoom"' in body                 # display cap
    assert '"updateWhenIdle": true' in body    # lazy: fetch SETTLED views
    assert "data-tile-lazy" in body            # the lazy-load marker attr
    assert body.count('integrity="sha384-') == 2  # js + css, pinned versions
    assert 'crossorigin="anonymous"' in body
    assert "defer" in body                     # never block the render
    assert 'href="https://unpkg.com"' in body
    assert 'href="https://tile.openstreetmap.org"' in body
    assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in body
    for banned in ("googleapis", "maps.google", "gstatic", "google.com"):
        assert banned not in body.lower()      # NO Google dependency in V1


def test_tile_budget_stub_is_pinned(world, db):
    assert tiling.TILE_BUDGET_BYTES == 2 * 1024 * 1024  # the 2 MiB stub
    assert tiling.TILE_LAYER_OPTIONS["updateWhenIdle"] is True
    assert tiling.MAX_NATIVE_ZOOM <= tiling.MAX_ZOOM    # fetch ≤ display
    note = tiling.tile_budget_note()
    assert note and "2 MiB" in note and "evict" in note.lower()
    page = Client().get(reverse("map:route")).content.decode()
    assert "2 MiB" in page  # the SW contract rides on the page itself


def test_tile_layer_never_doubles_bytes_on_retina(world, db):
    assert tiling.TILE_LAYER_OPTIONS["detectRetina"] is False


# =================================================================== budgets
def test_map_page_stays_under_the_50kb_budget(
    client, world, publishing_now, crowded_now, db
):
    assert_under_budget(
        client.get(
            f"{reverse('map:route')}?route={world['route_a'].pk}"
        ).content.decode(),
        50,
    )
    assert_under_budget(client.get(reverse("map:route")).content.decode(), 50)


# ================================================================== i18n/PII
def test_map_strings_are_gettext_marked():
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "apps" / "map"
    source = (app_dir / "views.py").read_text(encoding="utf-8")
    assert '_("' in source or "_(\n" in source, "views.py lacks gettext marks"
    text = (app_dir / "templates" / "map" / "map.html").read_text(encoding="utf-8")
    assert "{% trans" in text or "{% translate" in text


def test_no_crew_identity_in_any_map_surface(
    client, world, publishing_now, crowded_now, db
):
    payload = _positions_json(client, world["route_a"])
    blob = json.dumps(payload)
    page = client.get(
        f"{reverse('map:route')}?route={world['route_a'].pk}"
    ).content.decode()
    for bait in (
        "driver_bait_t28", "+254****0777", publishing_now["raw_token"], "mtk_",
        "authorized_by", "token_hash", "username",
    ):
        assert bait not in blob, f"identity leak in API: {bait}"
        assert bait not in page, f"identity leak on page: {bait}"


def test_privacy_rules_hold_for_map_reads(client, world, db):
    """The 72 h cut + boundary suppression hold when read THROUGH the map's
    views: three fresh fixes → only the middle one; an old fix → never."""
    session, token = tracking_services.start_session(
        world["veh_b"], by=world["admin"], now=VNOW
    )
    ingest.ingest_pings(
        token, session.pk, session.vehicle_id,
        [
            _ping(VNOW, 1, 0.0),
            _ping(VNOW, 2, 4.0),  # ~4 km east → the visible LIVE fix
            _ping(VNOW, 3, 8.0),  # end anchor
        ],
        now=VNOW + timedelta(minutes=4),
    )
    # An 80 h-old fix planted outside the ingest (pre-prune window):
    stale_at = VNOW - timedelta(hours=80)
    PingPoint.objects.create(  # noqa — direct write ONLY in a test plant
        session=session, lat=Decimal("-1.286400"), lon=Decimal("36.817200"),
        recorded_at=stale_at, received_at=stale_at,
    )
    payload = _positions_json(client, world["route_b"])
    (entry,) = payload["sessions"]
    assert len(entry["positions"]) == 1
    stale_iso = stale_at.isoformat()
    assert all(p["recorded_at"] != stale_iso for p in entry["positions"])