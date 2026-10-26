"""Prompt 03 acceptance: PostGIS present; geo helpers honest and correct.

These tests REQUIRE the PostGIS database (hard rule: geo code is never
tested against SQLite) and FAIL LOUDLY when it is missing — never skip.
"""
import re

import pytest

from apps.core import geo

NAIROBI_CBD = (-1.2864, 36.8172)
GIKOMBA = (-1.2841, 36.8350)      # ~2 km east of CBD
WESTLANDS = (-1.2672, 36.8067)   # ~4.5 km northwest of CBD
KAWANGWARE = (-1.3000, 36.7333)   # seed ward centroid
THIKA_TOWN = (-1.0450, 37.0710)  # ~35 km away: far outside the ward seed


@pytest.mark.django_db
def test_postgis_extension_available():
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT postgis_version()")
        version = cur.fetchone()[0]
    # NOTE: postgis_version() returns e.g. "3.6 USE_GEOS=1 USE_PROJ=1 ..." -
    # the word POSTGIS is NOT in it. Match a version pattern instead.
    assert re.match(r"^\d+\.\d+", str(version)), version


@pytest.mark.django_db
def test_distance_km_agrees_with_postgis():
    """Haversine must match PostGIS ST_Distance within 1%."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute(
            "SELECT ST_Distance("
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
            "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) / 1000.0",
            [WESTLANDS[1], WESTLANDS[0], KAWANGWARE[1], KAWANGWARE[0]],
        )
        pg_km = float(cur.fetchone()[0])
    py_km = geo.distance_km(WESTLANDS, KAWANGWARE)
    assert abs(py_km - pg_km) / pg_km < 0.01
    assert 5 < py_km < 15  # sanity: a cross-town hop, not a typo


@pytest.mark.django_db
def test_within_radius_sql_selects_expected_points():
    """The radius predicate returns exactly the in-range fixtures."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("CREATE TEMP TABLE geo_pts (id int, geog geography(Point, 4326))")
        cur.execute(
            "INSERT INTO geo_pts VALUES "
            "(1, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography), "
            "(2, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography), "
            "(3, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)",
            [
                NAIROBI_CBD[1], NAIROBI_CBD[0],
                GIKOMBA[1], GIKOMBA[0],
                THIKA_TOWN[1], THIKA_TOWN[0],
            ],
        )
        predicate = geo.within_radius_sql("geog", "lat", "lon", "km")
        cur.execute(
            f"SELECT id FROM geo_pts WHERE {predicate} ORDER BY id",
            {"lat": NAIROBI_CBD[0], "lon": NAIROBI_CBD[1], "km": 5.0},
        )
        ids = [row[0] for row in cur.fetchall()]
    assert ids == [1, 2]  # CBD + Gikomba inside 5 km; Thika excluded


def test_coarsen_quantizes_never_sub_km():
    aligned = (-1.296, 36.792)  # exact grid multiples of 0.018
    assert geo.coarsen(aligned) == aligned

    raw = (-1.2951, 36.7905)  # inside that cell
    snapped = geo.coarsen(raw)
    assert snapped == aligned
    assert geo.distance_km(raw, snapped) < 1.5  # within half a grid cell (~1 km)


def test_coarsen_moves_offgrid_points():
    raw = (-1.291234, 36.789123)
    snapped = geo.coarsen(raw)
    assert snapped != raw
    assert geo.distance_km(raw, snapped) < 1.5


def test_ward_label_nearest_within_seed():
    assert geo.ward_label((-1.2860, 36.8170)) == "Nairobi CBD"
    assert geo.ward_label((-1.2995, 36.7340)) == "Kawangware"


def test_ward_label_none_when_unknown():
    """Honesty: far-away points yield None — never a guessed label."""
    assert geo.ward_label(THIKA_TOWN) is None
