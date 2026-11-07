"""Ops door: refresh the DataMap registry from the hand inventory.

    python manage.py compliance_load_inventory

WHY a command (in ADDITION to the runtime loader on every dashboard hit
and the seed migration): ops/cron should be able to re-sync explicitly
after a deploy so the ODPC-facing map is current without needing a page
view first. Idempotent upsert; prints created/updated counts.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.compliance import services


class Command(BaseCommand):
    help = "Upsert the hand-maintained personal-data inventory into DataMapEntry."

    def handle(self, *args, **options):
        counts = services.load_inventory()
        self.stdout.write(self.style.SUCCESS(
            f"DataMap synced: created {counts['created']}, "
            f"updated {counts['updated']} rows."
        ))
