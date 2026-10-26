"""Geo utilities over PostGIS (PROMPTS.md Prompt 03).

DESIGN (ADR-0001 amendment): PostGIS is used through SQL functions on plain
lat/lon columns — no GeoDjango model fields — so no GDAL/GEOS system deps on
any dev host. Models keep numeric lon/lat; queries cast on the fly
(ST_MakePoint(...)::geography), or grow generated geography columns + GiST
indexes where scale demands it (Prompt 19).

CONVENTION: points are (lat, lon) tuples in Python — human/GPS order — but
ST_MakePoint takes (lon, lat) — PostGIS order. The helpers below are the ONLY
translation points, so the classic lat/lon swap bug cannot spread.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088

# ~2 km grid: 1 deg lat ~ 110.57 km; 1 deg lon ~ 111.32 km at Nairobi latitude.
GRID_STEP_DEG = 0.018

# Example ward seed — structure, not a gazetteer (PROMPTS.md Prompt 03).
# NOTE: deliberately tiny; later prompts give operators data-entry flows.
# Labels are never fabricated: unknown areas return None (brief §9 honesty).
WARDS: list[tuple[str, float, float]] = [
    # (name, lat, lon)
    ("Nairobi CBD", -1.2864, 36.8172),
    ("Westlands", -1.2672, 36.8067),
    ("Kawangware", -1.3000, 36.7333),
    ("Kilimani", -1.2921, 36.7727),
    ("Gikomba", -1.2841, 36.8350),
]


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance in km between (lat, lon) points.

    WHY pure Python: feed ranking and display math must not need a DB
    roundtrip. test_geo cross-checks this against PostGIS ST_Distance.
    """
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(h))


def coarsen(point: tuple[float, float]) -> tuple[float, float]:
    """Snap (lat, lon) to the ~2 km privacy grid.

    WHY: user locations are exposed ONLY at this granularity without
    explicit precise-location consent (KDPA minimization, hard rule).
    """
    lat, lon = point
    return (
        round(round(lat / GRID_STEP_DEG) * GRID_STEP_DEG, 6),
        round(round(lon / GRID_STEP_DEG) * GRID_STEP_DEG, 6),
    )


def ward_label(point: tuple[float, float], max_km: float = 15.0) -> str | None:
    """Nearest seeded ward within max_km, else None.

    HONESTY: None means "unknown around here" — callers display that;
    they never guess a label (product brief: no fabricated data).
    """
    best_name, best_km = None, max_km
    for name, wlat, wlon in WARDS:
        d = distance_km(point, (wlat, wlon))
        if d <= best_km:
            best_name, best_km = name, d
    return best_name


def geography_expr(lat_param: str, lon_param: str) -> str:
    """SQL fragment casting named params to geography.

    NOTE: ST_MakePoint is (x=lon, y=lat). This is THE lon/lat swap point.
    """
    return (
        f"ST_SetSRID(ST_MakePoint(%({lon_param})s, %({lat_param})s), 4326)::geography"
    )


def within_radius_sql(geog_col: str, lat_param: str, lon_param: str, km_param: str) -> str:
    """SQL predicate: geog_col within %(km)s km of (%(lat)s, %(lon)s).

    WHY geography not geometry: ST_DWould on geometry speaks degrees and
    silently returns nonsense distances; geography speaks meters
    (PROMPTS.md Prompt 03 pitfall).
    """
    return (
        f"ST_DWithin({geog_col}, "
        f"{geography_expr(lat_param, lon_param)}, "
        f"%({km_param})s * 1000)"
    )
