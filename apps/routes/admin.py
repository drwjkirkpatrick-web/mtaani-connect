"""Admin registration (Prompt 25): the stale 2018 seed stays EDITABLE.

NOTE on intent: every model registered so staff can fix seed-data errors
(names, coordinates, stage order, SACCO facts) directly — and the seeder
never overrides those edits on re-run (tested in tests/test_routes.py).
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    CorrectionQueue,
    CorrectionStatus,
    Fare,
    Route,
    RouteStage,
    Sacco,
    Stage,
    StageAlias,
)


@admin.register(Route)
class RouteAdmin(admin.ModelAdmin):
    list_display = ("name_en", "name_sw", "gtfs_id", "seed_origin", "shape_source")
    list_filter = ("seed_origin", "shape_source")
    search_fields = ("name_en", "name_sw", "gtfs_id")
    filter_horizontal = ("saccos",)
    # seed_quality note is a DISPLAY constant, not an editable field.


@admin.register(Sacco)
class SaccoAdmin(admin.ModelAdmin):
    list_display = ("name", "registration_status", "gtfs_id")
    list_editable = ("registration_status",)
    search_fields = ("name",)
    # contact_info editable here only — never exposed via public API (KDPA).


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ("name", "gtfs_id", "lat", "lon", "continuous_stop", "seed_origin")
    list_filter = ("seed_origin", "continuous_stop")
    search_fields = ("name", "aliases__alias")


@admin.register(StageAlias)
class StageAliasAdmin(admin.ModelAdmin):
    list_display = ("alias", "stage")
    search_fields = ("alias", "stage__name")


@admin.register(RouteStage)
class RouteStageAdmin(admin.ModelAdmin):
    list_display = ("route", "position", "stage")
    list_filter = ("route",)
    ordering = ("route", "position")


@admin.register(Fare)
class FareAdmin(admin.ModelAdmin):
    list_display = ("route", "from_stage", "to_stage", "min_price",
                    "max_price", "confidence", "observed_at")
    list_filter = ("confidence",)
    search_fields = ("route__name_en",)


@admin.register(CorrectionQueue)
class CorrectionQueueAdmin(admin.ModelAdmin):
    list_display = ("kind", "route", "stage", "opened_by", "status")
    list_filter = ("status", "kind")

    @admin.action(description=_("Mark selected corrections resolved"))
    def mark_resolved(self, request, queryset):
        from django.utils import timezone

        queryset.filter(status=CorrectionStatus.OPEN.value).update(
            status=CorrectionStatus.RESOLVED.value,
            resolution_note=_("Resolved in admin bulk action"),
            updated_at=timezone.now(),
        )

    actions = ("mark_resolved",)
