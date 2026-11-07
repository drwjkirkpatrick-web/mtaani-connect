from django.contrib import admin

from .models import FollowAlert, RouteFollow, VehicleFollow


@admin.register(VehicleFollow)
class VehicleFollowAdmin(admin.ModelAdmin):
    list_display = ("user", "vehicle", "notify_enabled", "notify_stage", "radius_km")
    list_filter = ("notify_enabled",)
    search_fields = ("user__username", "vehicle__plate", "vehicle__nickname")


@admin.register(RouteFollow)
class RouteFollowAdmin(admin.ModelAdmin):
    list_display = ("user", "route", "notify_enabled", "notify_stage", "radius_km")
    list_filter = ("notify_enabled",)
    search_fields = ("user__username", "route__name_en")


@admin.register(FollowAlert)
class FollowAlertAdmin(admin.ModelAdmin):
    list_display = ("follow_kind", "session", "stage", "notified_at", "adapter_id")
    list_filter = ("follow_kind",)
