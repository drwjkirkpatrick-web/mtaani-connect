"""Disputes admin (Prompt 23) — the human tools behind the code gates.

NOTE: these registrations exist so a superuser can always inspect cases,
evidence and flags; the DAILY flow is the disputes queue page, where the
aging tiers and the two-actor proposal gate live."""
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from . import services
from .models import (
    DisputeCase,
    DisputeProposal,
    EvidenceAttachment,
    FraudFlag,
)


@admin.register(DisputeCase)
class DisputeCaseAdmin(admin.ModelAdmin):
    list_display = ("deal_ref", "status", "opener", "age_display", "outcome")
    list_filter = ("status", "deal_app", "outcome")
    search_fields = ("what_promised", "what_happened")
    readonly_fields = ("opened_at",)

    @admin.display(description=_("Deal"))
    def deal_ref(self, obj):
        return f"{obj.deal_app}#{obj.deal_id}"

    @admin.display(description=_("Age"))
    def age_display(self, obj):
        tier = services.aging_tier(obj)  # read-time tier, never stored
        return getattr(obj, "opened_at", None) and tier or obj.status

    def has_delete_permission(self, request, obj=None):
        # Evidence retention: cases are decided, not erased.
        return False


@admin.register(DisputeProposal)
class DisputeProposalAdmin(admin.ModelAdmin):
    list_display = ("proposed_action", "case", "proposed_by", "confirmed_by")
    readonly_fields = ("confirmed_by", "confirmed_at")


@admin.register(EvidenceAttachment)
class EvidenceAttachmentAdmin(admin.ModelAdmin):
    list_display = ("case", "kind", "added_by", "created_at")
    readonly_fields = ("created_at",)


@admin.register(FraudFlag)
class FraudFlagAdmin(admin.ModelAdmin):
    list_display = ("heuristic", "target", "note", "created_at")
    list_filter = ("heuristic",)

    def has_delete_permission(self, request, obj=None):
        # Clearing a flag is a HUMAN decision on the live row, but admin
        # deletions would also orphan history — allowed for superusers,
        # the queue page is where confirmation/dismissal belongs.
        return True
