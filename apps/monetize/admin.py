"""Monetize admin (PROMPTS.md Prompt 38) — the staff row-management path
for everything the pages don't edit inline (registrations, label copy).
Every mutation runs THROUGH the services layer, so flips written from the
admin satisfy the same audit contract as page flips: services refuses
non-staff actors and writes the audit rows itself."""
from __future__ import annotations

from django.contrib import admin

from . import services
from .models import (
    FeatureFlag,
    MerchantEnrollment,
    MonetizationAudit,
    PromotedPlacement,
    SaccoPremiumTool,
)


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "enabled", "enabler", "enabled_at")
    list_filter = ("enabled",)
    search_fields = ("key", "label", "label_sw")

    @admin.action(description="Enable selected flags (audited)")
    def enable_selected(self, request, queryset):
        for flag in queryset:
            services.flip_flag(flag=flag, actor=request.user, enable=True)
        self.message_user(request, "Selected flags enabled (audited).")

    @admin.action(description="Disable selected flags (audited)")
    def disable_selected(self, request, queryset):
        for flag in queryset:
            services.flip_flag(flag=flag, actor=request.user, enable=False)
        self.message_user(request, "Selected flags disabled (audited).")

    actions = ("enable_selected", "disable_selected")


@admin.register(PromotedPlacement)
class PromotedPlacementAdmin(admin.ModelAdmin):
    list_display = (
        "listing", "slot", "active", "promoted_en", "promoted_sw", "enabled_at",
    )
    list_filter = ("slot", "active")
    raw_id_fields = ("listing",)

    @admin.action(description="Activate selected placements (audited)")
    def activate_selected(self, request, queryset):
        for placement in queryset:
            services.flip_placement(placement=placement, actor=request.user, enable=True)
        self.message_user(request, "Selected placements activated (audited).")

    @admin.action(description="Deactivate selected placements (audited)")
    def deactivate_selected(self, request, queryset):
        for placement in queryset:
            services.flip_placement(placement=placement, actor=request.user, enable=False)
        self.message_user(request, "Selected placements deactivated (audited).")

    actions = ("activate_selected", "deactivate_selected")


@admin.register(MerchantEnrollment)
class MerchantEnrollmentAdmin(admin.ModelAdmin):
    list_display = ("user", "tier", "tier_set_by", "tier_set_at")
    list_filter = ("tier",)

    @admin.action(description="Grant Merchant Pro to selected (audited)")
    def grant_pro(self, request, queryset):
        for enrollment in queryset:
            services.set_merchant_tier(
                enrollment=enrollment, actor=request.user, pro=True
            )
        self.message_user(request, "Selected merchants granted Pro (audited).")

    @admin.action(description="Revert selected to Free (audited)")
    def revert_free(self, request, queryset):
        for enrollment in queryset:
            services.set_merchant_tier(
                enrollment=enrollment, actor=request.user, pro=False
            )
        self.message_user(request, "Selected merchants reverted to Free (audited).")

    actions = ("grant_pro", "revert_free")


@admin.register(SaccoPremiumTool)
class SaccoPremiumToolAdmin(admin.ModelAdmin):
    list_display = ("sacco", "tool", "label_en", "enabled", "enabler", "enabled_at")
    list_filter = ("tool", "enabled")
    raw_id_fields = ("sacco",)

    @admin.action(description="Enable selected tools (audited)")
    def enable_selected(self, request, queryset):
        for tool in queryset:
            services.flip_sacco_tool(tool=tool, actor=request.user, enable=True)
        self.message_user(request, "Selected tools enabled (audited).")

    @admin.action(description="Disable selected tools (audited)")
    def disable_selected(self, request, queryset):
        for tool in queryset:
            services.flip_sacco_tool(tool=tool, actor=request.user, enable=False)
        self.message_user(request, "Selected tools disabled (audited).")

    actions = ("enable_selected", "disable_selected")


@admin.register(MonetizationAudit)
class MonetizationAuditAdmin(admin.ModelAdmin):
    """Read-only: audit rows are append-only evidence — no add/change/delete
    at any admin surface (the QuerySet guard blocks ORM-side rewrites too)."""

    list_display = ("surface", "action", "actor", "created_at", "note")
    list_filter = ("surface", "action")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff
