"""Seed admin-tunable tier thresholds (Prompt 16).

WHY a data migration: the spec says badges are a STRUCTURE with admin-tunable
thresholds — the rows must exist before any crossing can fire, but the
numbers are DATA, so admins can tune them later without a code change. Seeds
are per-trust-category × three levels; values are conservative "community
first" figures (lifetime receipts + recent receipts in the last 90 days).
The seeded rows are idempotent to re-apply (get_or_create on the UNIQUE
(category, level) pair) and unseed reverses exactly what it added.
"""
from django.db import migrations

LEVELS = ("RISING", "KNOWN", "TRUSTED")

# (category: [(min_lifetime, min_90d) per level, rising → trusted])
SEEDS = {
    "GENERAL": [(5, 2), (15, 6), (40, 15)],
    "MEDICAL": [(5, 2), (15, 6), (40, 15)],
    "ELECTRICAL": [(5, 2), (15, 6), (40, 15)],
    "CONSTRUCTION": [(5, 2), (15, 6), (40, 15)],
}


def seed(apps, schema_editor):
    TrustTier = apps.get_model("trust", "TrustTier")
    for category, rows in SEEDS.items():
        for level, (min_lifetime, min_90d) in zip(LEVELS, rows):
            TrustTier.objects.get_or_create(
                category=category,
                level=level,
                defaults={
                    "min_lifetime": min_lifetime,
                    "min_90d": min_90d,
                },
            )


def unseed(apps, schema_editor):
    # Reverse ONLY the exact seeded rows — admin-tuned rows survive.
    TrustTier = apps.get_model("trust", "TrustTier")
    for category, rows in SEEDS.items():
        for level, (min_lifetime, min_90d) in zip(LEVELS, rows):
            TrustTier.objects.filter(
                category=category,
                level=level,
                min_lifetime=min_lifetime,
                min_90d=min_90d,
            ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("trust", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
