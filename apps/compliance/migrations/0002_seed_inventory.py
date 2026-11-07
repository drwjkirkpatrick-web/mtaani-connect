"""Seed the DataMap registry from the hand-maintained inventory.

DATA MIGRATION, why: the registry must be complete on a FRESH database
before any request arrives (a new deploy cannot depend on a staff member
first visiting the dashboard to trigger the runtime upsert). The single
source of truth stays apps/compliance/inventory.py — importing it here
means future inventory edits land as ordinary code changes plus a NEW
seed revision (pattern: migrations.0002, 0003, …), never a silent edit
of this file.

NOTE: the runtime loader (services.load_inventory) remains the operating
mechanism at request time; this migration only guarantees the initial
sync after `migrate` for fresh installs.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from apps.compliance.services import load_inventory

    load_inventory()


def unseed(apps, schema_editor):
    # Reverse = clear the registry (rows are regenerable code, not data).
    DataMapEntry = apps.get_model("compliance", "DataMapEntry")
    DataMapEntry.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("compliance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
