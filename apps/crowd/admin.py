"""Admin for crowd reports (Prompt 30) — staff audit, not public surface.

Rider links are visible here (moderation/audit) but their identities are
never serialized into public payloads (see serialization.py contract).
"""

from django.contrib import admin

from .models import CrowdReport, CrowdReporterLink


@admin.register(CrowdReport)
class CrowdReportAdmin(admin.ModelAdmin):
    list_display = ("kind", "stage", "route", "merged_count", "last_bumped_at")
    list_filter = ("kind",)
    readonly_fields = ("merged_count", "last_bumped_at")
    search_fields = ("stage__name", "note")


@admin.register(CrowdReporterLink)
class CrowdReporterLinkAdmin(admin.ModelAdmin):
    list_display = ("report", "rider", "created_at")
    search_fields = ("rider__username",)
