"""Listing search & geo filters (Prompt 19) — PostGIS distance rings by ward.

THE HONESTY CONTRACT (KDPA 2019, read before editing anything here):

1. ``Listing`` carries a public ``ward`` TEXT ONLY — deliberately no lat/lon
   and no point column (apps/listings/models.py). Users never share their
   position for this feature, and listings must never grow coordinates.
2. Distance rings therefore operate at WARD GRANULARITY: the listing's ward
   text is matched (casefold) to a :class:`WardPoint` — an APPROXIMATE
   ward/area centroid in a reference table seeded by this app's data
   migration (public geographic knowledge, never survey-grade, never a user
   position). The filter measures centre-to-centre distance:
   ``ST_DWithin(WardPoint(listing.ward) point, WardPoint(picked area) point,
   km * 1000)``.
3. A listing whose ward matches no WardPoint is EXCLUDED from
   distance-filtered results — distance is never guessed (product brief §9:
   "no fabricated data"). The view's empty state names the wards a ring can
   measure today so the exclusion is visible, not silent.
4. Upgrade path: seeding more precise centroids (operator data-entry, later
   prompts) or an opt-in precise listing point (KDPA: logged consent) changes
   ONLY the seed/join inside this module — the SQL predicate (ST_DWithin on
   geography, 5/10/25 km rings) and the view keep their shape.

GEOMETRY DECISION (ADR-0001 amendment honesty): PostGIS is used through SQL
functions on plain lat/lon columns — no GeoDjango model fields, so no GDAL/
GEOS dependency on any dev host. ``WardPoint`` stores ``lat``/``lon`` floats;
the geography value is built on the fly by the helper below, and the GIST
index created in the migration indexes that same expression (the pitfall
PROMPTS.md names: the index must exist in the migration or every ring search
is a sequential scan). A generated geography column would also satisfy the
ADR wording; the expression index was chosen because it needs no new column
type on a non-postgis Django engine.

WHY raw SQL (same argument as apps/search/views.py): ``ST_DWithin`` is a
PostGIS operator — the plain-postgresql engine has no ORM spatial functions,
and inventing one would be worse than one honest parameterised read against
our OWN ``listings_wardpoint`` table.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import connection
from django.db import models
from django.db.models import UniqueConstraint
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.core.geo import geography_expr, within_radius_sql
from apps.core.types import TimestampedModel

from .models import CATEGORY_VALUES, Condition, Listing
from .services import visible_listings

# ---------------------------------------------------------------------------
# Prompt 38 SLOT — promoted listings, behind an explicit flag, invisible now.
#
# WHY a constant and not scattered comments: the parent spec says promoted
# listings "will slot in later behind an explicit flag, invisible for now" —
# this is the documented seam. search_listings() takes `promoted=False` today
# and treats it as a strict no-op (no rows are inserted, no ordering changes,
# nothing fabricated); Prompt 38 will read the flag to merge promoted rows
# into tier 0 of the ordering below, and this comment is where it says so.
# ---------------------------------------------------------------------------
PROMOTED_FLAG_COMMENT = (
    "PROMPT 38 SLOT (behind an explicit flag — invisible today): promoted "
    "listings will surface as tier 0 of the ordering in search_listings() "
    "when search_listings(promoted=True) gains its real implementation. "
    "Today the flag is a documented no-op: no promoted rows, no ranking "
    "hints, nothing fabricated. Revenue-adjacent state is built by humans "
    "(hard rule: monetization actions stay behind review gates)."
)

# Spec: exactly three rings, 5/10/25 km. Frozen tuple — the form, the
# validation and the tests all read this one constant, nobody re-types it.
RING_KM = (5, 10, 25)
RING_KM_SET = frozenset(RING_KM)

WARD_NAME_MAX_CHARS = 80

# ---------------------------------------------------------------------------
# Honest seed vocabulary (Prompt 19 spec): ten well-known Nairobi ward/
# neighbourhood centroids, public geographic knowledge — approximate by
# design, explicitly NOT survey-grade, and explicitly NOT user positions
# (users never share their position; this is a public reference table like
# apps/core/geo.py's WARDS). NOTE: values reuse apps.core.geo's published
# coordinates where that table already has an entry, so one project never
# publishes two different centroids for the same named area.
# ---------------------------------------------------------------------------
CENTROIDS: list[tuple[str, float, float]] = [
    # (name, lat, lon) — approximate, ±1 km by design
    ("Kawangware", -1.3000, 36.7333),
    ("Kilimani", -1.2921, 36.7727),
    ("Westlands", -1.2672, 36.8067),
    ("Umoja", -1.2900, 36.9080),
    ("Makadara", -1.3010, 36.8460),
    ("Ngara", -1.2830, 36.8230),
    ("Pangani", -1.2690, 36.8320),
    ("Kibera", -1.3130, 36.7790),
    ("Embakasi", -1.3170, 36.9000),
    ("South B", -1.3090, 36.8430),
]

MSG_UNKNOWN_CATEGORY = _(
    "Unknown category — it was ignored; pick one from the list. "
    "/ Kundi lisilojulikana limepuuzwa."
)
MSG_UNKNOWN_CONDITION = _(
    "Unknown condition value — it was ignored. "
    "/ Hali isiyojulikana imepuuzwa."
)
MSG_BAD_PRICE = _(
    "Price filters want a plain number of KSh — the odd value was ignored. "
    "/ Bei ni namba tatu tu."
)
MSG_BAD_RING = _(
    "Distance rings are 5, 10 or 25 km — the odd value was ignored. "
    "/ Rangi ni 5, 10 au 25 km tu."
)
MSG_KM_WITHOUT_AREA = _(
    "Pick a ward to search from first — the ring size measures FROM that "
    "ward. / Chagua Eneo kwanza."
)
MSG_AREA_WITHOUT_KM = _(
    "Pick a ring (5, 10 or 25 km) to search from that ward. "
    "/ Chagua rangi ya km."
)
MSG_NO_CENTRE = _(
    "Our list has no approximate centre for “%(value)s” yet — distance to "
    "an unmapped ward is never guessed. / Eneo halijulikani."
)


class WardPoint(TimestampedModel):
    """One APPROXIMATE ward/area centre — a reference table, not user data.

    WHY a table (not a constant): the ring filter needs indexed geometry AND
    the honest upgrade path in the module docstring (operators later seed
    more precise centroids without code changes). Rows hold public names as
    listings spell them, so the ward-join stays casefold-simple.

    NOTE: ``name`` is unique CASEFOLD (functional constraint) — "Kawangware"
    and "kawangware" must never become two different centres. The point
    itself lives in ``lat``/``lon`` floats; geography is composed on the fly
    (see module docstring — ADR-0001, no GeoDjango fields).
    """

    name = models.CharField(_("Ward / area name"), max_length=WARD_NAME_MAX_CHARS)
    # NOTE: approximate by design (±1 km) — labels say so; never survey-grade.
    lat = models.FloatField(_("Latitude (approximate)"))
    lon = models.FloatField(_("Longitude (approximate)"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("Ward centre (approximate)")
        verbose_name_plural = _("Ward centres (approximate)")
        constraints = [
            # WHY casefold, not case: ward text is typed by humans ("umoja",
            # "Umoja") — the join must treat them as the same centre, and the
            # same rule stops double-seeded rows.
            UniqueConstraint(Lower("name"), name="uniq_wardpoint_name_casefold"),
        ]

    def __str__(self) -> str:
        return self.name


def _wp_point(alias: str = "wp") -> str:
    """Row-side geography for a WardPoint: the COLUMN twin of geo.py's
    geography_expr(). ST_MakePoint is (x=lon, y=lat) — PostGIS order, the
    ONE lon/lat translation point for this module (same rule as geo.py).
    """
    lon, lat = f"{alias}.lon", f"{alias}.lat"
    return f"ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)::geography"


# The WardPoint side of the ring predicate — a column EXPRESSION, not a
# stored column: the migration's GIST index indexes exactly this expression,
# so planner and query always agree (WHY see _wp_point + module docstring).
WP_POINT = _wp_point("wp")
CENTRE_POINT = geography_expr("centre_lat", "centre_lon")
RING_PREDICATE = within_radius_sql(WP_POINT, "centre_lat", "centre_lon", "km")

# Public knowledge name of the migration-created index (EXPLAIN test asserts
# by THIS name — the pitfall's whole point: distance never runs without it).
RING_INDEX_NAME = "listings_wardpoint_ring_gist"

_RING_ROWS_SQL = f"""
SELECT wp.name,
       ST_Distance({WP_POINT}, {CENTRE_POINT}) AS dist_m
FROM listings_wardpoint wp
WHERE {RING_PREDICATE}
ORDER BY dist_m ASC, wp.name ASC
"""


def ring_sql(centre: "WardPoint", km: int) -> tuple[str, dict]:
    """(sql, params) for the ring read around a WardPoint centre — the exact
    statement production runs, exposed public so tests can EXPLAIN it (the
    Prompt 19 acceptance: index-served plan, not a sequential scan).
    """
    return _RING_ROWS_SQL, {"centre_lat": centre.lat, "centre_lon": centre.lon, "km": km}


def ring_wards(*, centre: "WardPoint", km: int) -> list[tuple[str, float]]:
    """Every WardPoint (name, centre-to-ward distance in km) within `km` km
    of `centre`. ST_DWithin INcludes the boundary (PostGIS <= semantics —
    tested against synthetic coordinates 8 km apart and at exactly 5 km).
    """
    sql, params = ring_sql(centre, km)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    return [(name, dist_m / 1000.0) for name, dist_m in rows]


def _relevance_tier(listing, *, category, min_price, max_price) -> int:
    """Relevance tier of one row: 1 = exact category match, 2 = inside the
    requested price band, 3 = the rest.

    WHY the result collapses under this UI: all four filter families are
    HARD filters ("filter by …", spec), so every row that comes back matches
    each requested anchor and one tier is uniform — ordering falls through
    to recency ("sorted by recency within relevance", spec). That honest
    collapse is the contract: this function computes real per-row tiers and
    invents NOTHING; it exists so that partial-anchor callers and Prompt 38's
    promoted tier 0 (see PROMOTED_FLAG_COMMENT) slot in without a rewrite.
    """
    if category and listing.category == category:
        return 1
    price_ok = listing.price is not None
    lo_ok = min_price is None or (price_ok and listing.price >= min_price)
    hi_ok = max_price is None or (price_ok and listing.price <= max_price)
    if (min_price is not None or max_price is not None) and lo_ok and hi_ok:
        return 2
    return 3


def _to_decimal(value) -> Decimal | None:
    """Stray querystrings must degrade, never crash or mislead (same policy
    as listing_list's fallback). None = not a usable price."""
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def search_listings(
    *,
    viewer=None,
    category: str | None = None,
    condition: str | None = None,
    min_price=None,
    max_price=None,
    area: str | None = None,
    km=None,
    promoted: bool = False,
) -> tuple[list[Listing], dict]:
    """The Prompt 19 discovery read: category, price band, condition and a
    5/10/25 km ring around a PICKED area (never the viewer's position).

    Returns (results ordered tier-then-recency, meta) where meta carries the
    honest search context: ``ring_applied`` / ``area_name`` / ``km`` /
    ``available_areas`` / ``excluded_unknown_wards`` / ``notices`` /
    ``total``. View layers compose the human copy from this structure so the
    empty state can name which ward centres a ring can measure.

    WHY the visible policy comes from services.visible_listings(): one
    market rule everywhere (PUBLISHED + ACTIVE, blocked sellers hidden) —
    cross-app moderation behaviour is never re-implemented here.
    """
    notices: list[str] = []
    available_areas = list(
        WardPoint.objects.order_by("name").values_list("name", flat=True)
    )
    meta: dict = {
        "ring_applied": False,
        "area_name": None,
        "km": None,
        "available_areas": available_areas,
        "excluded_unknown_wards": [],
        "notices": notices,
        "total": 0,
        # NOTE (Prompt 38): never True today — see PROMOTED_FLAG_COMMENT.
        "promoted_applied": False,
    }

    # --- anchors: stray values degrade to honest notices, never a 500 ------
    category = (category or "").strip() or None
    if category is not None and category not in CATEGORY_VALUES:
        notices.append(MSG_UNKNOWN_CATEGORY)
        category = None
    condition = (condition or "").strip() or None
    if condition is not None and condition not in Condition.values:
        notices.append(MSG_UNKNOWN_CONDITION)
        condition = None
    min_price_raw, min_price = min_price, _to_decimal(min_price) if min_price not in (None, "") else None
    max_price_raw, max_price = max_price, _to_decimal(max_price) if max_price not in (None, "") else None
    # HONESTY: a value that parses to nothing is DROPPED and NAMED — the
    # search must not silently pretend the user's band never existed.
    if min_price_raw not in (None, "") and min_price is None:
        notices.append(MSG_BAD_PRICE)
    if max_price_raw not in (None, "") and max_price is None:
        notices.append(MSG_BAD_PRICE)
    if isinstance(km, str):
        km = km.strip()
    try:
        km = int(km) if km not in (None, "") else None
    except (TypeError, ValueError):
        km = None
    # Ring intent is captured BEFORE any zeroing — the honest-empty rule
    # below must see what the viewer asked for, not what survived parsing.
    km_wanted = km is not None
    if km is not None and km not in RING_KM_SET:
        notices.append(MSG_BAD_RING)
        km = None
    area = (area or "").strip()
    area_wanted = bool(area)
    centre = None
    if area_wanted:
        centre = WardPoint.objects.filter(name__iexact=area).first()
        if centre is None:
            # HONESTY: never guess a distance for an unmapped ward.
            notices.append(MSG_NO_CENTRE % {"value": area})
        elif km is None:
            notices.append(MSG_AREA_WITHOUT_KM % {"value": centre.name})
    elif km_wanted:
        notices.append(MSG_KM_WITHOUT_AREA)
        km = None

    # WHAT THE GUARD MEANS: the viewer asked for a distance view the module
    # cannot honour — an unmapped ward, or an area with no ring size, or a
    # ring size with no area. Silently returning the WHOLE market would
    # pretend the request succeeded; an honest EMPTY list (with the named
    # notices above, which the empty state renders) is the answer.
    if (area_wanted or km_wanted) and not (centre is not None and km is not None):
        meta["total"] = 0
        return [], meta

    # --- base public surface + hard filters ---------------------------------
    qs = visible_listings(viewer=viewer, category=category)
    if condition:
        qs = qs.filter(condition=condition)
    if min_price is not None:
        # WHY NULL-safe naturally: price IS NULL (negotiable-only) fails both
        # range predicates → such rows drop out of a banded search, which is
        # the honest answer ("unknown price inside band?" is not knowable).
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)

    results = list(qs)  # ONE read; select_related("seller") from visible_listings

    # --- distance ring (ward-to-ward, honest exclusion) ----------------------
    if centre is not None and km is not None:
        wards_by_casefold = {
            name.strip().casefold(): dist_km for name, dist_km in ring_wards(centre=centre, km=km)
        }
        ringed, unknown_wards = [], []
        for listing in results:
            ward_km = wards_by_casefold.get((listing.ward or "").strip().casefold())
            if ward_km is None:
                unknown_wards.append(listing.ward)
                continue  # never guessed → excluded (empty state explains)
            # Ward-level constant: listings of one ward share it (a display
            # value, not a fabricated position — the label says "approximate").
            listing.ring_distance_km = ward_km
            ringed.append(listing)
        results = ringed
        meta["ring_applied"] = True
        meta["area_name"] = centre.name
        meta["km"] = km
        meta["excluded_unknown_wards"] = sorted(set(unknown_wards))[:8]

    # --- ordering: relevance tier first, then recency ------------------------
    results.sort(
        key=lambda l: (
            _relevance_tier(l, category=category, min_price=min_price, max_price=max_price),
            -( (l.bumped_at or l.created_at).timestamp() ),
        )
    )
    # WHY no third key row: visible_listings already ordered by
    # (-recency, -created_at) in SQL; the stable Python sort keeps exactly
    # that order for equal (tier, recency) keys — deterministic, one rule.

    meta["total"] = len(results)
    return results, meta
