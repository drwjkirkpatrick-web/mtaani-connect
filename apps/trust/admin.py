"""Admin for the Trust app (Prompt 16) — the "admin-tunable" surface.

WHY: TrustTier is the only EDITABLE thing here — threshold rows are data, so
tuning who counts as rising/known/trusted never touches code (PROMPTS.md 16:
"no hardcoded persona content"). Votes and badge events are read-only records.
"""
from django.contrib import admin

from .models import BadgeEvent, HelpfulVote, TrustTier


@admin.register(TrustTier)
class TrustTierAdmin(admin.ModelAdmin):
    list_display = ("category", "level", "min_lifetime", "min_90d")
    list_filter = ("category", "level")
    search_fields = ("category",)
    ordering = ("category", "min_lifetime")


@admin.register(HelpfulVote)
class HelpfulVoteAdmin(admin.ModelAdmin):
    list_display = ("voter", "category", "target_ct", "target_id", "created_at")
    list_filter = ("category", "target_ct")
    raw_id_fields = ("voter",)
    # NOTE: votes are never edited from the admin (one row per user/target is
    # an integrity contract, not an editorial field).

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(BadgeEvent)
class BadgeEventAdmin(admin.ModelAdmin):
    list_display = ("user", "category", "level", "awarded_at", "created_at")
    list_filter = ("category", "level")
    raw_id_fields = ("user",)
    # WHY read-only too: badge provenance is an audit trail for Prompt 22;
    # admin edits would rewrite trust history.

    def has_change_permission(self, request, obj=None):
        return False
