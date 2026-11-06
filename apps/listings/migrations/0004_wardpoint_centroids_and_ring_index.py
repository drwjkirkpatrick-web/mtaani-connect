"""Prompt 19 — WardPoint reference table, approximate centroid seed, GIST
ring index. One migration, wholly additive (models.py untouched: Listing
still has no geometry — KDPA ward-only).

WHY the index lives here (spec pitfall): the ring filter runs
``ST_DWithin`` against the WardPoint geography EXPRESSION
``ST_SetSRID(ST_MakePoint("lon","lat"),4326)::geography`` — exactly the
expression this statement indexes — so a distance search can always use an
index scan instead of a sequential scan. ADR-0001 (apps/core/geo.py):
PostGIS through SQL functions on plain lat/lon columns, no GeoDjango fields,
no GDAL/GEOS on dev hosts; a generated geography column would be the other
ADR-sanctioned shape — the expression index was chosen because it adds no
column type a non-postgis Django engine has to model.

WHY ``core`` is an explicit dependency: the ``postgis`` extension is created
by apps/core migration 0001; the GIST index cannot build before it exists.

Seed honesty: the ten WardPoints are APPROXIMATE public ward/area centroids
(±1 km by design, not survey-grade, nothing user-supplied — users never
share their position for this feature). Distance is therefore ever only
WARD-LEVEL: see apps/listings/search.py's module docstring contract.
Reverse ops drop OUR index/table but never touch shared extensions.
"""
from django.db import migrations, models
from django.db.models import UniqueConstraint
from django.db.models.functions import Lower


def _seed_ward_centroids(apps, schema_editor):
    # WHY get_or_create per row: idempotent under re-runs, and never
    # overwrites a later, better-precision row an operator added by hand.
    from apps.listings.search import CENTROIDS  # noqa: PLC0415 — local by design

    WardPoint = apps.get_model("listings", "WardPoint")
    for name, lat, lon in CENTROIDS:
        WardPoint.objects.get_or_create(name=name, defaults={"lat": lat, "lon": lon})


class Migration(migrations.Migration):

    dependencies = [
        ("listings", "0003_listing_category_gate"),
        ("core", "0001_initial"),  # the postgis extension — see docstring
    ]

    operations = [
        migrations.CreateModel(
            name="WardPoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=80, verbose_name="Ward / area name")),
                ("lat", models.FloatField(verbose_name="Latitude (approximate)")),
                ("lon", models.FloatField(verbose_name="Longitude (approximate)")),
            ],
            options={
                "ordering": ["name"],
                "verbose_name": "Ward centre (approximate)",
                "verbose_name_plural": "Ward centres (approximate)",
                # NOTE: constraints live INSIDE options for CreateModel
                # (Django's own generated migrations shape).
                "constraints": [
                    UniqueConstraint(
                        Lower("name"),
                        name="uniq_wardpoint_name_casefold",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX listings_wardpoint_ring_gist ON listings_wardpoint "
                "USING GIST ((ST_SetSRID(ST_MakePoint(\"lon\", \"lat\"), 4326)::geography))"
            ),
            reverse_sql="DROP INDEX IF EXISTS listings_wardpoint_ring_gist",
        ),
        migrations.RunPython(_seed_ward_centroids, migrations.RunPython.noop),
    ]
