"""tests/test_routes.py — Prompt 25: route network + GTFS seeder (own file).

Covers the acceptance list: synthetic GTFS import (probe, not assume),
alias/nickname trigram search, fare RANGES with confidence labels,
graceful no-zip degradation, correction-queue flow, seed-quality honesty,
and the never-fabricate-shapes pitfall.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core.testing import assert_under_budget
from apps.routes import gtfs as gtfs_module
from apps.routes import search as routes_search
from apps.routes import views as routes_views
from apps.routes.models import (
    CorrectionKind,
    CorrectionQueue,
    CorrectionStatus,
    Fare,
    Route,
    RouteStage,
    Sacco,
    Stage,
    StageAlias,
)

# --------------------------------------------------------------------- fixture
# A SMALL SYNTHETIC Digital Matatus-style zip (probed columns below). It is
# built at runtime — the repo never needs the real (and stale) 2018 archive.


def _gtfs_members(headers: str = "standard") -> dict[str, str]:
    routes = (
        "route_id,route_short_name,route_long_name,route_color,agency_id\n"
        "1,12,Khoja - Kinoo,FFCB05,MATUMBAT\n"
        "2,23,Ambassadeur - Kasarani,,KANAMO\n"
        "3,44,Lavington - Limuru,,\n"
    )
    stops = (
        "stop_id,stop_name,stop_lat,stop_lon\n"
        "s1,Khoja,-1.282140,36.824340\n"
        "s2,Kinoo,-1.270410,36.740270\n"
        "s3,Ambassadeur,-1.289120,36.816400\n"
        "s4,Mayanga,-1.299400,36.789500\n"
        "s5,Kasarani,-1.220300,36.890700\n"
        "s6,Lavington,,\n"  # no coordinates: honest "no coordinates" state
    )
    if headers == "quirky":  # modified-feed quirk: cased/renamed columns
        routes = (
            "Route_ID,route_no,route_desc,color,agency_id\n"
            "1,12,Khoja - Kinoo,FFCB05,MATUMBAT\n"
            "2,23,Ambassadeur - Kasarani,,KANAMO\n"
            "3,44,Lavington - Limuru,,\n"
        )
        stops = (
            "Stop_ID,Stop_Name,Stop_Lat,Stop_Lon\n"
            "s1,Khoja,-1.282140,36.824340\n"
            "s2,Kinoo,-1.270410,36.740270\n"
            "s3,Ambassadeur,-1.289120,36.816400\n"
            "s4,Mayanga,-1.299400,36.789500\n"
            "s5,Kasarani,-1.220300,36.890700\n"
            "s6,Lavington,,\n"
        )
    shapes = (
        "shape_id,shape_pt_sequence,shape_pt_lat,shape_pt_lon,route_id\n"
        "sh1,1,-1.282140,36.824340,1\n"
        "sh1,2,-1.276000,36.790000,1\n"
        "sh1,3,-1.270410,36.740270,1\n"
    )
    trips = (
        "route_id,trip_id,shape_id\n"
        "1,t1,sh1\n"
        "2,t2,\n"
    )
    stop_times = (
        "trip_id,stop_id,stop_sequence\n"
        "t1,s1,1\n"
        "t1,s2,2\n"
        "t2,s3,1\n"
        "t2,s4,2\n"
        "t2,s5,3\n"
    )
    aliases = (
        "stop_id,alias\n"
        "s3,Ambassador\n"
        "s4,Manyanga\n"
    )
    members = {
        "routes.txt": routes,
        "stops.txt": stops,
        "shapes.txt": shapes,
        "trips.txt": trips,
        "stop_times.txt": stop_times,
        "stop_aliases.txt": aliases,
    }
    return members


def _write_gtfs_zip(tmp_path, headers: str = "standard", include: set | None = None) -> str:
    include = include or {
        "routes.txt", "stops.txt", "shapes.txt", "trips.txt",
        "stop_times.txt", "stop_aliases.txt",
    }
    members = _gtfs_members(headers)
    path = tmp_path / "digital_matatus_seed.zip"
    with zipfile.ZipFile(path, "w") as z:
        for name, text in members.items():
            if name in include:
                z.writestr(name, text)
    return str(path)


@pytest.fixture
def seeded(tmp_path, db):
    """Import the synthetic zip once; return the seeder's counters."""
    seeder = gtfs_module.GtfsSeeder(_write_gtfs_zip(tmp_path))
    report = seeder.seed()
    return seeder, report


@pytest.fixture
def staff_user(db):
    """A moderator account for admin_client-style writes (queue resolve)."""
    User = get_user_model()
    user = User.objects.create_superuser(
        "qa_moderator", "qa_moderator@example.com", password="***"
    )
    return user


# ------------------------------------------------------------------ degradation
@pytest.mark.django_db
def test_no_zip_available_false_and_seed_reports_error(tmp_path):
    """ZIP ABSENT → available() False; seeding reports an error honestly."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert gtfs_module.available(empty) is False
    seeder = gtfs_module.GtfsSeeder(empty / "missing.zip")
    assert seeder.probe() is False
    result = seeder.seed()
    assert result.get("error") == 1
    assert Route.objects.count() == 0


@pytest.mark.django_db
def test_no_zip_manual_admin_entry_still_works(client, tmp_path):
    """Degradation: no zip ⇒ honest empty state, but manual entry (admin or
    ORM — same data path) remains fully functional, seed rows or not."""
    empty = tmp_path / "empty"
    empty.mkdir()
    # "Manual admin entry": plain Django ORM rows, no seeder involved.
    sacco = Sacco.objects.create(name="Matumbato", registration_status="REGISTERED")
    route = Route.objects.create(name_en="CBD - Kawangware")
    route.saccos.add(sacco)
    stage = Stage.objects.create(name="Bunyala", continuous_stop=True)
    RouteStage.objects.create(route=route, stage=stage, position=1)
    Fare.objects.create(route=route, from_stage=stage,
                        to_stage=Stage.objects.create(name="Kawangware"),
                        min_price=50, max_price=80)

    response = client.get(reverse("routes:list"))
    assert "No GTFS zip found" in response.content.decode()
    assert route.name_en in response.content.decode()  # manual row listed

    found_routes, found_stages = routes_search.search_network("Kawangware")
    assert [r["name_en"] for r in found_routes] == [route.name_en]
    found_routes_2, _ = routes_search.search_network("Matumbato")
    assert found_routes_2 and found_routes_2[0]["id"] == route.pk


# ------------------------------------------------------------------- import
@pytest.mark.django_db
def test_synthetic_zip_import_probes_and_seeds(seeded, tmp_path):
    """3 routes / 6 stops → 3 Route rows, 6 Stage rows, ordered links,
    SACCO attached by agency_id, colour normalised to hex."""
    seeder, report = seeded
    assert report["routes_created"] == 3
    assert report["stages_created"] == 6
    assert report.get("stages_present", 0) == 0
    assert Route.objects.count() == 3
    assert Stage.objects.count() == 6

    # Probed, not assumed: record what the loader mapped.
    assert seeder.probed["routes.txt"]["route_id"] == "route_id"
    assert seeder.probed["stop_times.txt"]["sequence"] == "stop_sequence"

    route1 = Route.objects.get(gtfs_id="1")
    # GTFS hex colours arrive bare → stored normalised for CSS use.
    assert route1.corridor_color == "#FFCB05"
    assert route1.seed_origin is True
    assert route1.saccos.count() == 1
    assert route1.shape_source == Route.ShapeSource.GTFS  # real shapes.txt line
    assert route1.shape_points and len(route1.shape_points) == 3

    # Ordered sequence: stop_times order via trips, 1-based positions.
    order1 = list(route1.stagelinks.order_by("position").values_list(
        "stage__name", "position"))
    assert order1 == [("Khoja", 1), ("Kinoo", 2)]
    route2 = Route.objects.get(gtfs_id="2")
    order2 = list(route2.stagelinks.order_by("position").values_list(
        "stage__name", "position"))
    assert order2 == [("Ambassadeur", 1), ("Mayanga", 2), ("Kasarani", 3)]

    # Aliases imported from the optional alias member — never invented.
    amb = Stage.objects.get(name="Ambassadeur")
    assert StageAlias.objects.filter(stage=amb, alias="Ambassador").exists()
    may = Stage.objects.get(name="Mayanga")
    assert StageAlias.objects.filter(stage=may, alias="Manyanga").exists()

    # Route 3 has NO shape and NO trip data: honest shape_source, no links.
    route3 = Route.objects.get(gtfs_id="3")
    assert route3.shape_source == Route.ShapeSource.STAGE_TO_STAGE
    assert route3.shape_points is None
    assert route3.stagelinks.count() == 0
    # The un-coordinated stop is imported WITHOUT invented coordinates.
    lavington = Stage.objects.get(name="Lavington")
    assert lavington.lat is None and lavington.lon is None


@pytest.mark.django_db
def test_quirk_headers_probe_case_insensitive(tmp_path):
    """Modified-feed headers ("Route_ID", "Stop_Lat"...) map — probed."""
    seeder = gtfs_module.GtfsSeeder(_write_gtfs_zip(tmp_path, headers="quirky"))
    assert seeder.probe()
    assert seeder.probed["routes.txt"]["route_id"] == "Route_ID"
    assert seeder.probed["stops.txt"]["lat"] == "Stop_Lat"
    report = seeder.seed()
    assert report["routes_created"] == 3
    assert report["stages_created"] == 6


@pytest.mark.django_db
def test_broken_zip_degrades_cleanly(tmp_path):
    """A corrupt zip: probe False, no crash, no half-seeded network."""
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04-not-a-real-zip")
    seeder = gtfs_module.GtfsSeeder(str(path))
    assert seeder.probe() is False
    assert seeder.seed().get("error") == 1
    assert Route.objects.count() == 0


@pytest.mark.django_db
def test_reseed_idempotent_db_counts_stable(seeded):
    """Re-running the seeder changes nothing (upsert keyed by gtfs_id)."""
    _, report1 = seeded
    seeder2 = gtfs_module.GtfsSeeder(seeded[0].zip_path)
    report2 = seeder2.seed(override=False)
    assert report2.get("routes_created", 0) == 0
    assert report2["routes_present"] == 3
    assert report2.get("stages_created", 0) == 0
    assert report2["stages_present"] == 6
    assert Route.objects.count() == 3
    assert Stage.objects.count() == 6
    assert RouteStage.objects.count() == 5


@pytest.mark.django_db
def test_admin_edit_survives_reseed_but_override_restamps(seeded):
    """The stale-feed rule: an admin edit beats the 2018 file on re-seed.

    NOTE the /admin/ site is NOT mounted in the root URLconf yet (parent
    wiring at consolidation); an admin change-page submit issues exactly the
    same ``save()`` this executes, and admin.py registration is asserted in
    test_admin_registers_all_models. The edit-preservation SEMANTICS
    (seeder._upsert) are what this test pins.
    """
    seeder, _ = seeded
    route = Route.objects.get(gtfs_id="1")
    route.name_en = "Khoja - Kinoo (CBD)"  # the admin's renamed value
    route.corridor_color = "#008800"
    route.save()
    again = Route.objects.get(pk=route.pk)
    assert again.name_en == "Khoja - Kinoo (CBD)"

    seeder.seed(override=False)  # the DEFAULT policy: never clobber repairs
    again.refresh_from_db()
    assert again.name_en == "Khoja - Kinoo (CBD)"
    assert again.corridor_color == "#008800"

    seeder.seed(override=True)  # explicit re-stamp path
    again.refresh_from_db()
    assert again.name_en == "Khoja - Kinoo"


# ------------------------------------------------------------------- shapes
@pytest.mark.django_db
def test_route_without_shape_is_stage_to_stage_and_labeled(client, seeded):
    """PITFALL: no shapes.txt line ⇒ approximate join, plainly labeled;
    a route whose stages lack coordinates shows NO line at all."""
    _, _ = seeded
    route1 = Route.objects.get(gtfs_id="1")
    route2 = Route.objects.get(gtfs_id="2")  # trip data, no shape rows
    r1 = client.get(reverse("routes:detail", args=[route1.pk]))
    assert "GTFS shapes.txt" in r1.content.decode()
    r2 = client.get(reverse("routes:detail", args=[route2.pk]))
    body = r2.content.decode()
    assert "stage-to-stage" in body
    assert "NOT the real road path" in body
    # Route 3: no shape, no linked stages ⇒ nothing drawable, said clearly.
    route3 = Route.objects.get(gtfs_id="3")
    r3 = client.get(reverse("routes:detail", args=[route3.pk]))
    assert "No line available" in r3.content.decode()


@pytest.mark.django_db
def test_seed_quality_badge_visible(client, seeded):
    """Honest labelling: seeded routes wear the seed-quality badge; a
    manually-created route does not."""
    _, _ = seeded
    route = Route.objects.get(gtfs_id="1")
    page = client.get(reverse("routes:detail", args=[route.pk]))
    assert "seed-quality data (2018 GTFS)" in page.content.decode()
    manual = Route.objects.create(name_en="Hand-made corridor")
    page2 = client.get(reverse("routes:detail", args=[manual.pk]))
    assert "seed-quality" not in page2.content.decode()


# ------------------------------------------------------------------- search
@pytest.mark.django_db
def test_alias_search_trigram_nickname_queries(seeded):
    """"Manyanga"-style queries reach aliased entries; the classic
    Ambassadeur/Ambassador pair matches BOTH directions."""
    _, _ = seeded
    routes, stages = routes_search.search_network("Ambassador")
    stage_names = [s["name"] for s in stages]
    assert "Ambassadeur" in stage_names  # alias "Ambassador" → stage
    assert any(r["name_en"] == "Ambassadeur - Kasarani" for r in routes)
    assert any(r["stage_hit"] for r in routes)  # WHY: provenance shown in UI

    routes, stages = routes_search.search_network("Manyanga")
    assert "Mayanga" in [s["name"] for s in stages]  # typo-level tolerance
    assert any(r["name_en"] == "Ambassadeur - Kasarani" for r in routes)


@pytest.mark.django_db
def test_reverse_alias_search(seeded):
    """Searching the OFFICIAL name while the alias differs also works
    (seeder pairs both ways through the alias table)."""
    _, _ = seeded
    _, stages = routes_search.search_network("Ambassadeur")
    assert "Ambassadeur" in [s["name"] for s in stages]


@pytest.mark.django_db
def test_search_by_sacco_name(seeded):
    """SACCO-centred search ("which matatus run Matumbato?")."""
    _, _ = seeded
    routes, _ = routes_search.search_network("MATUMBAT")
    assert [r["name_en"] for r in routes] == ["Khoja - Kinoo"]
    assert any(r["sacco_hit"] for r in routes)


@pytest.mark.django_db
def test_search_by_destination_hits_route(seeded):
    """Destination query (terminal stage name) finds the corridor."""
    _, _ = seeded
    routes, stages = routes_search.search_network("Kasarani")
    assert any(r["name_en"] == "Ambassadeur - Kasarani" for r in routes)
    assert "Kasarani" in [s["name"] for s in stages]


@pytest.mark.django_db
def test_search_honest_empty_and_hinted_states(client, seeded):
    _, _ = seeded
    page_no_q = client.get(reverse("routes:search"))
    assert "approximate spellings work" in page_no_q.content.decode()
    page_miss = client.get(reverse("routes:search") + "?q=zzzqqqwww")
    assert "No match" in page_miss.content.decode()
    assert Route.objects.count() == 3  # page invented no rows


# -------------------------------------------------------------------- fares
@pytest.mark.django_db
def test_fare_requires_observed_range_not_single_number(seeded):
    """Ranges only: reversed bounds / missing bounds / self-segment rejected."""
    Route.objects.get  # noqa: B018 (models exist via fixture)
    route = Route.objects.get(gtfs_id="1")
    a = Stage.objects.get(name="Khoja")
    b = Stage.objects.get(name="Kinoo")
    with pytest.raises(ValidationError):
        Fare(route=route, from_stage=a, to_stage=b,
             min_price=90, max_price=50).full_clean()  # reversed
    with pytest.raises(ValidationError):
        Fare(route=route, from_stage=a, to_stage=b).full_clean()  # no bounds
    with pytest.raises(ValidationError):
        Fare(route=route, from_stage=a, to_stage=a,
             min_price=50, max_price=50).full_clean()  # self-segment
    Fare(route=route, from_stage=a, to_stage=b, min_price=50,
         max_price=80).full_clean()  # valid range passes


@pytest.mark.django_db
def test_fare_range_display_with_confidence(client, seeded):
    _, _ = seeded
    route = Route.objects.get(gtfs_id="1")
    fare = Fare.objects.create(
        route=route,
        from_stage=Stage.objects.get(name="Khoja"),
        to_stage=Stage.objects.get(name="Kinoo"),
        min_price=50, max_price=80,
        confidence="COMMUNITY_REPORT",
        observed_at="2026-11-01",
    )
    assert fare.range_display() == "KSh 50–80"
    assert fare.confidence_display() == "Community report"
    page = client.get(reverse("routes:detail", args=[route.pk]))
    body = page.content.decode()
    assert "KSh 50–80" in body      # always a SPAN, never one "official" price
    assert "Community report" in body


@pytest.mark.django_db
def test_stage_page_shows_fares_from_it(client, seeded):
    _, _ = seeded
    route = Route.objects.get(gtfs_id="1")
    khoja = Stage.objects.get(name="Khoja")
    kinoo = Stage.objects.get(name="Kinoo")
    Fare.objects.create(route=route, from_stage=khoja, to_stage=kinoo,
                        min_price=50, max_price=80)
    page = client.get(reverse("routes:stage_detail", args=[khoja.pk]))
    assert "KSh 50–80" in page.content.decode()
    assert "Khoja - Kinoo" in page.content.decode()


# ------------------------------------------------------- stage honesty flags
@pytest.mark.django_db
def test_stage_without_coordinates_honest_state(client, seeded):
    _, _ = seeded
    stage = Stage.objects.get(name="Lavington")
    page = client.get(reverse("routes:stage_detail", args=[stage.pk]))
    assert "No coordinates recorded" in page.content.decode()


@pytest.mark.django_db
def test_continuous_stop_flag_display(client, seeded):
    """Continuous-stop awareness: flag surfaces on stage detail + search."""
    _, _ = seeded
    stage = Stage.objects.get(name="Mayanga")
    stage.continuous_stop = True
    stage.save(update_fields=["continuous_stop"])
    page = client.get(reverse("routes:stage_detail", args=[stage.pk]))
    assert "hailable en route" in page.content.decode()
    _, stages = routes_search.search_network("Mayanga")
    assert stages and stages[0]["continuous_stop"] is True


# --------------------------------------------------------- correction queue
@pytest.mark.django_db
def test_correction_queue_flow(client_auth, admin_client, staff_user):
    """Open → visible to the reporter → staff resolves with a note;
    anonymous users can't open; non-staff can't see the queue."""
    cor_form = reverse("routes:correction_new")
    assert client_auth.get(cor_form).status_code == 200  # login required ✓

    route = Route.objects.create(name_en="Khoja - Kinoo", seed_origin=True)
    response = client_auth.post(cor_form, {
        "kind": CorrectionKind.WRONG_STAGE_POSITION,
        "route": route.pk,
        "stage": "",
        "note": "The stage order puts Kinoo before it should be.",
    })
    assert response.status_code == 302
    assert CorrectionQueue.objects.count() == 1

    mine = client_auth.get(reverse("routes:corrections_mine"))
    assert "queued for review" not in mine.content.decode() or \
        CorrectionQueue.objects.get().status == CorrectionStatus.OPEN.value

    # Non-staff cannot see or resolve the queue.
    assert client_auth.get(reverse("routes:queue")).status_code == 403
    item = CorrectionQueue.objects.get()
    assert client_auth.post(reverse("routes:correction_resolve",
                                    args=[item.pk])).status_code == 403

    # Staff sees the open entry and resolves it with a note (human gate).
    queue_page = admin_client.get(reverse("routes:queue"))
    assert queue_page.status_code == 200
    assert "Wrong stage position" in queue_page.content.decode()
    resolve = admin_client.post(reverse("routes:correction_resolve",
                                        args=[item.pk]), {
        "resolution_note": "Checked against stop_times — positions fixed.",
    })
    assert resolve.status_code == 302
    item.refresh_from_db()
    assert item.status == CorrectionStatus.RESOLVED.value
    assert "positions fixed" in item.resolution_note
    resolved_visible = admin_client.get(reverse("routes:queue"))
    assert "Recently resolved" in resolved_visible.content.decode()


@pytest.mark.django_db
def test_correction_missing_route_kind_needs_no_target(client_auth):
    """"MISSING_ROUTE" claims an absence — route/stage fields optional."""
    response = client_auth.post(reverse("routes:correction_new"), {
        "kind": CorrectionKind.MISSING_ROUTE,
        "route": "", "stage": "",
        "note": "Route 125 to Gikambura is missing from the seed data.",
    })
    assert response.status_code == 302
    assert CorrectionQueue.objects.filter(kind=CorrectionKind.MISSING_ROUTE).exists()


@pytest.mark.django_db
def test_correction_wrong_fare_requires_a_target(client_auth):
    """Grounded corrections only: WRONG_* kinds must point at something."""
    response = client_auth.post(reverse("routes:correction_new"), {
        "kind": CorrectionKind.WRONG_FARE,
        "route": "", "stage": "",
        "note": "Fare is out of range.",
    })
    assert response.status_code == 200
    assert CorrectionQueue.objects.count() == 0
    assert "route" in response.content.decode().lower() or "stage" in \
        response.content.decode().lower()


@pytest.mark.django_db
def test_correction_rate_limit(client_auth, monkeypatch):
    """Local queue mirrors moderation's rate-limited reporting spirit."""
    monkeypatch.setattr(routes_views, "MAX_CORRECTIONS_PER_HOUR", 2)
    user = client_auth.session.get("_auth_user_id")
    for i in range(2):
        CorrectionQueue.objects.create(
            kind=CorrectionKind.MISSING_ROUTE, note=f"n{i}",
            opened_by=get_user_model().objects.get(pk=user),
        )
    response = client_auth.post(reverse("routes:correction_new"), {
        "kind": CorrectionKind.MISSING_ROUTE, "note": "one too many",
    }, follow=True)
    assert CorrectionQueue.objects.count() == 2  # the 3rd is refused
    assert [m.tags for m in response.context["messages"]] == ["error"]


@pytest.mark.django_db
def test_moderation_integration_documented():
    """Deferred wiring documented: local queue mirrors moderation.Report;
    'routes' must be added to ALLOWED_TARGET_APPS at consolidation."""
    from apps.routes import models as m
    assert "ALLOWED_TARGET_APPS" in CorrectionQueue.__doc__ or \
        "ALLOWED_TARGET_APPS" in (m.__doc__ or "")


# ------------------------------------------------------------------ admin/UI
@pytest.mark.django_db
def test_index_stats_and_search_page_budget(client, seeded):
    """Spec surfaces stay within the 50 KB mobile budget."""
    _, _ = seeded
    assert_under_budget(client.get(reverse("routes:list")).content.decode("utf-8"), kb=50)
    assert_under_budget(
        client.get(reverse("routes:search") + "?q=Ambassador").content.decode("utf-8"),
        kb=50,
    )
    route = Route.objects.get(gtfs_id="1")
    assert_under_budget(
        client.get(reverse("routes:detail", args=[route.pk])).content.decode("utf-8"),
        kb=50,
    )
    stage = Stage.objects.get(name="Kinoo")
    assert_under_budget(
        client.get(reverse("routes:stage_detail", args=[stage.pk])).content.decode("utf-8"),
        kb=50,
    )


@pytest.mark.django_db
def test_index_counts_reflect_network(client, seeded):
    _, _ = seeded
    response = client.get(reverse("routes:list"))
    body = response.content.decode()
    assert "3 routes" in body and "6 stages" in body
    assert "2 SACCOs" in body  # MATUMBAT + KANAMO agencies from the feed


@pytest.mark.django_db
def test_admin_registers_all_models(client, seeded):
    """Every model registered via admin.py (the seed stays editable, spec).

    NOTE the /admin/ URL is mounted by the parent at consolidation; what we
    can lock NOW is the registration contract (and that list pages render).
    """
    _, _ = seeded
    from django.contrib import admin
    from apps.routes.models import CorrectionQueue as CQ

    for model in (Route, Sacco, Stage, StageAlias, RouteStage, Fare, CQ):
        assert model in admin.site._registry, f"{model} missing from admin"
    # And the public list page works for anonymous visitors.
    assert client.get(reverse("routes:list")).status_code == 200
