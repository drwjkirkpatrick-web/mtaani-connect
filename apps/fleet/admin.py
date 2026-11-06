"""Admin registration (Prompt 26): operator state stays staff-editable.

NOTE the seat of power: SaccoAdmin rows are created here ONLY by staff (the
admin site is staff-only) — the same human gate services.grant_sacco_admin
enforces programmatically. FleetAudit is read-only: a trail you can edit is
not a trail.
"""
from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .models import (
    CrewMember,
    CrewStatus,
    FleetAudit,
    SaccoAdmin,
    Vehicle,
    VehicleStatus,
)


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("plate", "nickname", "sacco", "capacity", "status")
    list_filter = ("status", "sacco")
    search_fields = ("plate", "nickname", "sacco__name")
    filter_horizontal = ("routes",)


@admin.register(CrewMember)
class CrewMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "vehicle", "role", "status", "approved_by", "approved_at")
    list_filter = ("status", "role")

    @admin.action(description=_("Return selected seats to pending"))
    def reopen_pending(self, request, queryset):
        # Staff correction path only: approval/revocation facts stay intact
        # elsewhere; here rows must go back to a clean pending state.
        queryset.update(
            status=CrewStatus.PENDING.value,
            approved_by=None,
            approved_at=None,
            revoked_by=None,
            revoked_at=None,
        )

    actions = ("reopen_pending",)


@admin.register(SaccoAdmin)
class SaccoAdminProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "sacco", "added_by", "created_at")
    search_fields = ("user__username", "sacco__name")


@admin.register(FleetAudit)
class FleetAuditAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "sacco", "vehicle", "crew", "created_at")
    list_filter = ("action",)
    # Read-only on purpose (docstring): editing an audit trail destroys it.
    def has_change_permission(self, request, obj=None) -> bool:  # noqa: D103
        return False
