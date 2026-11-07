"""Seed the three master monetization flags, OFF (PROMPTS.md Prompt 38).

WHY a data migration: the acceptance is "monetization is armed but pointed
at nothing until the team flips flags deliberately" — on a FRESH database
the flags page must already show every switch OFF with nobody having
enabled it and NO audit rows yet (a seeding is not a flip). Values are the
registry in apps/monetize/services (copied LITERAL here — migrations must
not import live modules; test_monetize asserts the two stay in parity).

Rows are idempotent to re-apply (get_or_create on the unique key) and the
unseed function reverses exactly the seeded rows — admin-tuned labels
survive.
"""
from django.db import migrations

SEEDS = (
    (
        "promoted_search_inline",
        "Promoted listings inside search results",
        "Matangazo yaliyopandishwa kwenye matokeo ya utafutaji",
    ),
    (
        "merchant_tiers",
        "Merchant subscription tiers (features, not grants)",
        "Daraja za ushirika kwa wauzaji (vipimo, si utoaji)",
    ),
    (
        "sacco_premium_tools",
        "Premium tools for SACCOs (registry stays stubbed)",
        "Zana maalum kwa magari ya kikundi (bado ni mfano)",
    ),
)


def seed(apps, schema_editor):
    FeatureFlag = apps.get_model("monetize", "FeatureFlag")
    for key, label, label_sw in SEEDS:
        FeatureFlag.objects.get_or_create(
            key=key,
            # ALWAYS off at creation: enabled has its schema default (False)
            # and enabler/enabled_at stay NULL — nobody enabled a seed.
            defaults={"label": label, "label_sw": label_sw},
        )


def unseed(apps, schema_editor):
    # Reverse ONLY the exact seeded rows — staff-flipped flags survive.
    FeatureFlag = apps.get_model("monetize", "FeatureFlag")
    for key, label, label_sw in SEEDS:
        FeatureFlag.objects.filter(key=key, label=label, label_sw=label_sw).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("monetize", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
