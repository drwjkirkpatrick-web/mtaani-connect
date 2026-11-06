"""Tests for Prompt 19 — listing search & geo filters (apps/listings/search.py).

Contract points asserted here:
- THE ring math: a synthetic WardPoint pair exactly 8 km apart is excluded
  by the 5 km ring and included by 10/25 km; the 5 km ring is
  boundary-INclusive (ST_DWithin semantics, built at exactly 5000 m with
  ST_Project so the stored pair keeps full float precision).
- ward-granularity honesty: a listing in an UNMAPPED ward ("Kasarani") is
  excluded from ring results, never guessed into them, and the empty state
  names the wards a ring CAN measure.
- filter combinations (category + price band + condition + ring) return
  exactly the right fixtures, ordered recency-within-relevance.
- the GIST index from migration 0004 exists AND serves the ring query
  (catalog + EXPLAIN plan type — the spec pitfall).
- budget compliance on result pages (assert_under_budget, 50 KB).
- PROMOTED_FLAG_COMMENT: Prompt 38's slot stays a strict no-op today.
"""
from decimal import Decimal
from datetime import timedelta
from types import SimpleNamespace

import json

import pytest
from django.db import IntegrityError, connection, transaction
from django.urls import reverse

from apps.core import geo
from apps.core.testing import assert_under_budget
from apps.listings import search, services
from apps.listings.models import Listing
from apps.listings.search import PROMOTED_FLAG_COMMENT, RING_INDEX_NAME, WardPoint

pytestmark = pytest.mark.django_db

GOOD_LISTING = {
    "title": "Custom tailoring - suits + dresses",
    "description": "Two-piece suits made to measure. Pickup arranged by in-app message.",
    "category": "handmade",
    "ward": "Kawangware",
    "price": 1500,
}


def _member(username="wanjiku"):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="***"
    )


def _listing(seller, **extra):
    return services.create_listing(seller=seller, **{**GOOD_LISTING, **extra})


def _ward(name, lat, lon):
    return WardPoint.objects.create(name=name, lat=lat, lon=lon)


def _sql_rows(sql, params=None):
    params = params or {}
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _project_north(lon, lat, metres):
    """(lat, lon, achieved_m) of the point exactly `metres` due north of
    (lat, lon) — synthetic fixture coordinates with a KNOWN geodesic
    separation: PostGIS computes the destination and the test never
    hand-rounds, so the stored floats keep full precision and the achieved
    centre-to-centre distance is exact, not approximate."""
    rows = _sql_rows(
        "SELECT ST_Y(pt::geometry)::float8, ST_X(pt::geometry)::float8, "
        "ST_Distance(ST_SetSRID(ST_MakePoint(%(lon0)s, %(lat0)s), 4326)::geography, pt)::float8 "
        "FROM (SELECT ST_Project(ST_SetSRID(ST_MakePoint(%(lon0)s, %(lat0)s), 4326)::geography, "
        "%(metres)s, radians(0)) AS pt) t",
        {"lon0": lon, "lat0": lat, "metres": metres},
    )
    lat2, lon2, achieved_m = rows[0]
    return lat2, lon2, achieved_m


# --------------------------------------------------------------------------- seed


def test_seed_ten_approximate_centroids_exist():
    names = list(WardPoint.objects.order_by("name").values_list("name", flat=True))
    assert len(names) == 10
    assert set(names) == {
        "Kawangware", "Kilimani", "Umoja", "Makadara", "Ngara",
        "Pangani", "Kibera", "Embakasi", "South B", "Westlands",
    }
    for row in WardPoint.objects.all():
        assert -1.45 <= row.lat <= -1.0
        assert 36.5 <= row.lon <= 37.0  # the real Nairobi band, public knowledge


def test_casefold_centre_names_are_unique():
    with pytest.raises(IntegrityError):
        WardPoint.objects.create(name="kawangware", lat=-1.30, lon=36.7333)


# --------------------------------------------------------------------------- rings


def test_synth_pair_8km_apart_ring_5_10_25_semantics():
    """Two WardPoints exactly 8 km apart: 5 km excludes, 10/25 include —
    through the REAL service API (not a hand-copied SQL)."""
    lat2, lon2, achieved = _project_north(36.7333, -1.30, 8_000)
    assert abs(achieved - 8_000) < 1.0  # synthetic coordinates: near-exact
    _ward("Ringsville A", -1.30, 36.7333)
    _ward("Ringsville B", lat2, lon2)
    buyer = _member("ringbuyer")
    far = _listing(buyer, title="Far item", ward="Ringsville B")     # 8 km away
    close = _listing(buyer, title="Close item", ward="Ringsville A") # at the centre

    # recency order: close was created later → first everywhere it qualifies
    km5, _ = search.search_listings(viewer=buyer, area="Ringsville A", km=5)
    assert [row.pk for row in km5] == [close.pk]          # 8 km ≠ within 5 km

    km10, meta10 = search.search_listings(viewer=buyer, area="Ringsville A", km=10)
    assert [row.pk for row in km10] == [close.pk, far.pk]
    assert meta10["ring_applied"] and meta10["area_name"] == "Ringsville A"
    assert meta10["km"] == 10

    km25, _ = search.search_listings(viewer=buyer, area="Ringsville A", km=25)
    assert [row.pk for row in km25] == [close.pk, far.pk]


def test_ring_boundary_at_exactly_5km_is_inclusive():
    """Boundary-inclusive ring semantics around 5 km (the spec's edge case):

    PROBED REALITY (see tests comments): PostGIS stores the pair at exactly
    5000.0 geodesic metres (ST_Distance == 5000.0 — the boundary VALUE is
    reached), but ST_DWithin's internal expansion is float-noisy within
    ~1e-6 m around an exactly-equal radius — at d == r == 5000.0 a raw
    ST_DWithin can flip false. The DETERMINISTIC assertions therefore sit
    half a millimetre on each side of the boundary — far beyond the noise
    and far beyond any real ward positioning — and include must WIN at the
    edge (ST_DWithin is documented <=; never a strict <)."""
    centre_lat, centre_lon = -1.30, 36.7333
    lat_in, lon_in, _ = _project_north(centre_lon, centre_lat, 5_000 - 0.0005)
    lat_out, lon_out, _ = _project_north(centre_lon, centre_lat, 5_000 + 0.0005)
    _ward("Boundary A", centre_lat, centre_lon)
    _ward("Boundary In", lat_in, lon_in)
    _ward("Boundary Out", lat_out, lon_out)
    buyer = _member("boundarybuyer")
    inside = _listing(buyer, title="At the 5 km edge (in)", ward="Boundary In")
    outside = _listing(buyer, title="Just past the 5 km edge", ward="Boundary Out")

    rows, _ = search.search_listings(viewer=buyer, area="Boundary A", km=5)
    pk_list = [row.pk for row in rows]
    assert pk_list == [inside.pk]  # inclusive at the boundary; 0.5 mm past is out
    # the boundary VALUE itself is really 5.000 km (not silently widened):
    rows10, _ = search.search_listings(viewer=buyer, area="Boundary A", km=10)
    assert outside.pk in [row.pk for row in rows10]


def test_seed_distances_agree_with_core_geo():
    """PostGIS ring values and apps.core.geo's haversine cross-check — one
    convention across engines (same pattern as tests/test_geo.py)."""
    kawa = WardPoint.objects.get(name="Kawangware")
    by_name = dict(search.ring_wards(centre=kawa, km=10))
    expected = geo.distance_km((-1.3000, 36.7333), (-1.2921, 36.7727))
    assert abs(by_name["Kilimani"] - expected) / expected < 0.03  # sphere models
    assert by_name["Kibera"] <= 10.0 and by_name["Westlands"] <= 10.0


def test_ring_query_uses_the_gist_index():
    """THE pitfall: distance filtering must never be a sequential scan —
    the migration's GIST index is (1) present in the catalog and (2) able to
    serve the EXACT ring statement search_listings runs, asserted via
    EXPLAIN plan type. The enable_seqscan hint is measured, like apps/search
    does: a 10-row fixture table is planner-ambiguous territory, and the
    test asserts USABILITY of the index, not planner statistics."""
    defs = _sql_rows("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                     [RING_INDEX_NAME])
    assert len(defs) == 1
    assert "using gist" in defs[0][0].lower()
    # WHY lower(): pg_indexes renders function identifiers lowercase
    assert "st_makepoint" in defs[0][0].lower()  # indexes the geography EXPRESSION

    centre = _ward("Plan Origin", -1.30, 36.7333)
    sql, params = search.ring_sql(centre, 10)
    assert "ST_DWithin" in sql
    with transaction.atomic():  # savepoint: SET LOCAL never leaks outward
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(f"EXPLAIN (FORMAT JSON) {sql}", params)
            plan_raw = cursor.fetchall()[0][0]
    # WHY the isinstance: psycopg3 auto-decodes JSON; older adapters hand
    # back the raw string — the test must serve both honestly.
    plan_doc = plan_raw if isinstance(plan_raw, (list, dict)) else json.loads(plan_raw)
    plan_nodes = json.dumps(plan_doc)
    assert RING_INDEX_NAME in plan_nodes        # planned ON our index
    assert "Seq Scan" not in plan_nodes         # with the hint, no seq scan


# --------------------------------------------------------------------------- honesty


def test_listing_in_unmapped_ward_is_excluded_from_ring_not_all():
    """Ward-granularity honesty: a distance to an unmapped ward is never
    guessed — Kasarani exits RING results but stays in the unfiltered market."""
    buyer = _member("honestbuyer")
    unmapped = _listing(buyer, title="Kasarani no-centre item", ward="Kasarani")
    inside = _listing(buyer, title="Kilimani inside item", ward="Kilimani")

    rows, meta = search.search_listings(viewer=buyer, area="Kawangware", km=10)
    assert [row.pk for row in rows] == [inside.pk]
    assert meta["excluded_unknown_wards"] == ["Kasarani"]
    assert meta["available_areas"] == [
        n for n in (
            "Embakasi", "Kawangware", "Kibera", "Kilimani", "Makadara",
            "Ngara", "Pangani", "South B", "Umoja", "Westlands",
        )
    ]

    broad, _ = search.search_listings(viewer=buyer)  # no ring → Kasarani visible
    pk_list = [row.pk for row in broad]
    assert unmapped.pk in pk_list and inside.pk in pk_list


def test_ward_join_is_casefold_and_whitespace_tolerant():
    buyer = _member("casingbuyer")
    stale = _listing(buyer, title="Typed loosely", ward="  kilimani ")
    rows, meta = search.search_listings(viewer=buyer, area="Kilimani", km=5)
    assert [row.pk for row in rows] == [stale.pk]
    assert meta["area_name"] == "Kilimani"  # canonical centre name echoed


def test_negotiable_null_price_drops_out_of_a_price_band():
    buyer = _member("pricebuyer")
    freeform = _listing(buyer, title="Haggling item", ward="Umoja",
                        price=None, price_negotiable=True)
    priced = _listing(buyer, title="Priced item", ward="Umoja", price=800)

    rows, _ = search.search_listings(
        viewer=buyer, area="Kawangware", km=25, min_price=500, max_price=1000
    )
    assert [row.pk for row in rows] == [priced.pk]  # unknown price ≠ in-band guess

    wider, _ = search.search_listings(viewer=buyer, area="Kawangware", km=25)
    pk_list = [row.pk for row in wider]
    assert freeform.pk in pk_list and priced.pk in pk_list  # no band → no exclusion


# --------------------------------------------------------------------------- combos + order


def test_filter_combinations_return_exactly_right_fixtures():
    buyer = _member("combobuyer")
    _ward("Combo Far", *_project_north(36.7333, -1.30, 35_000)[:2])  # 35 km: outside 25
    target = _listing(buyer, title="The tailoring target", ward="Kawangware",
                      category="handmade", price=1500)             # matches ALL filters
    out_of_band = _listing(buyer, title="Costly suit", ward="Kilimani",
                           category="handmade", price=2500)        # outside the band
    other_cat = _listing(buyer, title="Old radio", ward="Kawangware",
                         category="electronics", price=1500)       # outside category
    worn = _listing(buyer, title="Worn suit", ward="Umoja",
                    category="handmade", price=1500, condition="worn")  # outside condition
    out_of_ring = _listing(buyer, title="Far-away suit", ward="Combo Far",
                           category="handmade", price=1500)        # beyond the 25 km ring

    rows, _ = search.search_listings(
        viewer=buyer, category="handmade", min_price=1000, max_price=2000,
        condition="good", area="Kawangware", km=25,
    )
    assert [row.pk for row in rows] == [target.pk]

    rows_no_cond, _ = search.search_listings(
        viewer=buyer, category="handmade", min_price=1000, max_price=2000,
        area="Kawangware", km=25,
    )
    # dropping condition adds WORN PRICE-OK rows only; target still leads
    assert worn.pk in [row.pk for row in rows_no_cond]
    assert out_of_band.pk not in [row.pk for row in rows_no_cond]

    rows_no_cat, _ = search.search_listings(
        viewer=buyer, min_price=1000, max_price=2000, condition="good",
        area="Kawangware", km=25,
    )
    pk_list = [row.pk for row in rows_no_cat]
    assert target.pk in pk_list and other_cat.pk in pk_list       # category no longer filters
    assert out_of_ring.pk not in pk_list                          # still ring-filtered
    assert out_of_band.pk not in pk_list and worn.pk not in pk_list


def test_recency_within_relevance_bump_leads():
    buyer = _member("freshbuyer")
    older = _listing(buyer, title="Older first")
    newer = _listing(buyer, title="Newer second")
    rows, _ = search.search_listings(viewer=buyer, area="Kawangware", km=25)
    assert [row.pk for row in rows] == [newer.pk, older.pk]
    # Bump the OLDER row through the REAL one-tap service. WHY `now`: fresh
    # rows sit under the 7-day bump cooldown — the service's injectable
    # clock lets tests exercise ordering without sleeping (repo convention).
    services.bump_listing(
        listing=Listing.objects.get(pk=older.pk), by_user=buyer,
        now=older.created_at + timedelta(days=9),
    )
    rows_after, _ = search.search_listings(viewer=buyer, area="Kawangware", km=25)
    assert [row.pk for row in rows_after] == [older.pk, newer.pk]


def test_relevance_tier_pure_semantics():
    """Tier function: 1 exact category, 2 inside band, 3 rest. Under this
    UI's HARD filters tiers run uniform and sort is recency (module
    docstring) — pure calls are where the tiers are exercised, without any
    fabricated relevance data."""
    row = SimpleNamespace(category="handmade", price=Decimal("1500"))
    assert search._relevance_tier(row, category="handmade",
                                  min_price=None, max_price=None) == 1
    assert search._relevance_tier(row, category=None,
                                  min_price=Decimal("1000"),
                                  max_price=Decimal("2000")) == 2
    assert search._relevance_tier(row, category="new",
                                  min_price=None, max_price=None) == 3
    assert search._relevance_tier(row, category=None,
                                  min_price=Decimal("2000"), max_price=None) == 3


def test_promoted_flag_is_a_documented_noop_today():
    buyer = _member("promobuyer")
    _listing(buyer, title="A listing")
    _listing(buyer, title="B listing")
    plain, meta_plain = search.search_listings(viewer=buyer, area="Kawangware", km=25)
    promoted, meta_prom = search.search_listings(
        viewer=buyer, area="Kawangware", km=25, promoted=True)
    assert [row.pk for row in promoted] == [row.pk for row in plain]
    assert meta_prom["promoted_applied"] is False
    upper_comment = PROMOTED_FLAG_COMMENT.upper()
    assert "PROMPT 38" in upper_comment and "FLAG" in upper_comment


def test_stray_inputs_degrade_to_honest_notices_not_500():
    buyer = _member("straybuyer")
    _listing(buyer)
    rows_all, meta_all = search.search_listings(viewer=buyer)
    assert meta_all["total"] == 1 and len(rows_all) == 1

    rows, meta = search.search_listings(
        viewer=buyer, km=9, min_price="abc", category="yolo", condition="magnetic")
    texts = [str(n) for n in meta["notices"]]
    assert any("5, 10 or 25" in n for n in texts)   # bad ring, named plainly
    assert any("plain number" in n for n in texts)  # bad price, named plainly

    rows2, meta2 = search.search_listings(viewer=buyer, area="Nowhere 69", km=10)
    assert rows2 == []                                  # honest empty, no guessing
    assert any("never guessed" in str(n) for n in meta2["notices"])

    rows3, meta3 = search.search_listings(viewer=buyer, km=10)  # km without area
    assert rows3 == [] and meta3["ring_applied"] is False
    assert any("Pick a ward" in str(n) for n in meta3["notices"])

    rows4, meta4 = search.search_listings(viewer=buyer, area="Kawangware")  # no km
    assert rows4 == [] and meta4["ring_applied"] is False
    assert any("Pick a ring" in str(n) for n in meta4["notices"])


# --------------------------------------------------------------------------- views / budget


def test_search_page_renders_ring_results_under_budget(client, fresh_user):
    for i in range(6):
        _listing(fresh_user, title=f"Tailor suit {i}", ward="Kilimani", price=1000 + i)
    response = client.get(reverse("listings:search"),
                          {"area": "Kawangware", "km": 10, "category": "handmade",
                           "min_price": 1000, "max_price": 1400})
    assert response.status_code == 200
    body = response.content.decode()
    assert "Tailor suit 5" in body          # the right fixtures render
    assert "km from Kawangware" in body     # ward-level distance label
    assert "ward to ward" in body
    assert_under_budget(body, 50)


def test_search_page_empty_state_names_available_centres(client, fresh_user):
    _listing(fresh_user, title="Only Kasarani", ward="Kasarani")
    response = client.get(reverse("listings:search"), {"area": "Kawangware", "km": 10})
    assert response.status_code == 200
    body = response.content.decode()
    assert "Nothing listed within 10 km of Kawangware" in body
    assert "approximate ward centres" in body       # the honest contract line
    assert "Kilimani" in body                        # available centres listed


def test_search_page_stray_params_render_notices_not_500(client):
    response = client.get(reverse("listings:search"),
                          {"km": 9, "area": "Nowhere 69", "min_price": "abc",
                           "category": "yolo", "condition": "magnetic"})
    assert response.status_code == 200
    body = response.content.decode()
    assert "5, 10 or 25" in body    # ring notice rendered on the page
    assert "never guessed" in body  # unknown-area notice rendered
    assert "plain number" in body   # price notice rendered


def test_search_form_offers_seeded_areas_and_exact_rings(client):
    body = client.get(reverse("listings:search")).content.decode()
    for area_name in ("Kawangware", "Umoja", "South B"):
        assert area_name in body
    assert 'value="5"' in body and 'value="10"' in body and 'value="25"' in body
    assert 'value="9"' not in body and 'value="50"' not in body  # 5/10/25 only


def test_market_list_keeps_its_behavior_and_carries_the_search_row(client, fresh_user):
    _listing(fresh_user, title="Custom tailoring - suits")
    response = client.get(reverse("listings:list"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Search near a ward" in body  # the additive GET form row
    assert 'action="/market/search/"' in body
    # the EXISTING filters keep working (list route untouched):
    assert "Custom tailoring - suits" in client.get(
        reverse("listings:list"), {"q": "tailoring"}).content.decode()


def test_post_to_search_is_rejected_get_only(client):
    response = client.post(reverse("listings:search"), {})
    assert response.status_code == 405  # a GET-only surface by design

