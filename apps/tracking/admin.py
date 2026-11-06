"""Admin for Prompt 27 — staff-side review surface for the privacy core.

WHAT staff may see: authorization facts, moments, cap, token HASH (never a
raw token — display-once is absolute; there is no "peek" because showing
again would be storing). No inline ping table here: positions are the
query layer's business (visible_positions), and dumping raw pings in admin
would bypass Rule 3's boundary suppression for eyes that don't need them.
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import PingPoint, TrackingSession


@admin.register(TrackingSession)
class TrackingSessionAdmin(admin.ModelAdmin):
    empty_value_display = _("(none yet)")  # gettext'd: a staff-facing string
    list_display = (
        "vehicle", "started_at", "ends_at", "cap_hours",
        "authorization_source", "ended_reason",
    )
    list_filter = ("authorization_source", "ended_reason")
    search_fields = ("vehicle__plate",)

    def has_add_permission(self, request):
        # WHY not addable: sessions start ONLY through the authorization
        # doors (services.start_session) — a hand-made row would have a
        # token the service's digest contract doesn't own.
        return False


@admin.register(PingPoint)
class PingPointAdmin(admin.ModelAdmin):
    list_display = ("session", "recorded_at", "received_at", "lat", "lon")
    search_fields = ("session__vehicle__plate",)
    # Retention belongs to prune_pings, not to manual staff edits.
    actions = None
