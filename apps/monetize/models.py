"""Monetization models (PROMPTS.md Prompt 38) — scaffolding, OFF by default.

Acceptance: monetization is ARMED but pointed at NOTHING until the team
flips flags deliberately:
- `FeatureFlag.enabled` defaults FALSE and is seeded false by a data
  migration; enabler/enabled_at stay NULL until a staff member flips.
- surfaces (promoted placements, merchant tiers, SACCO premium tools) each
  carry the same disabled-default provenance trio.
- every deliberate flip writes ONE append-only `MonetizationAudit` row
  (surface, action, actor, target, at, note) — mirrors of the current state
  live on the surface rows, HISTORY lives only in audit rows.
- NOTHING grants itself: flips exist ONLY as services (staff-guarded, see
  services.py) or as Django-admin actions that call those services. Model
  clean() blocks an "enabled" row that does not record who enabled it.

NO PAYMENTS PROCESSING — billing is a future prompt once demand is proven;
nothing here prices, bills, or stores any payment instrument.

I18N NOTE: no gettext markers anywhere in this app — the project sw catalog
under locale/ is a SHARED file (Prompt 35's drift detector fails the suite
on any new code msgid) that monetize does not own. Bilingual copy ships as
stored en+sw CharFields (the spec's own mechanism) + labels.py constants;
verbose names below are PLAIN strings for the same reason. See labels.py.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.types import TimestampedModel

from .labels import PROMOTED_LABEL_EN, PROMOTED_LABEL_SW, SACCO_TOOL_LABEL_EN, SACCO_TOOL_LABEL_SW

MASTER_SEED_LABELS = {
    # (key -> (label en, label sw)) — the three master switches. Seeded OFF
    # by migration; the flip service re-reads labels only for get_or_create.
    "promoted_search_inline": (
        "Promoted listings inside search results",
        "Matangazo yaliyopandishwa kwenye matokeo ya utafutaji",
    ),
    "merchant_tiers": (
        "Merchant subscription tiers (features, not grants)",
        "Daraja za ushirika kwa wauzaji (vipimo, si utoaji)",
    ),
    "sacco_premium_tools": (
        "Premium tools for SACCOs (registry stays stubbed)",
        "Zana maalum kwa magari ya kikundi (bado ni mfano)",
    ),
}

FLAG_SEARCH_INLINE = "promoted_search_inline"
FLAG_MERCHANT_TIERS = "merchant_tiers"
FLAG_SACCO_TOOLS = "sacco_premium_tools"

MAX_KEY_CHARS = 64
MAX_LABEL_CHARS = 120
MAX_NOTE_CHARS = 240
MAX_LABEL_COPY_CHARS = 64
MAX_TOOL_LABEL_CHARS = 120

# The audit's generic-target allow-list: monetize audits only its own
# surfaces. Stretching a security log across apps couples both (fleet's
# local-audit WHY) — the FK limit + save-guard keeps this local and typed.
ALLOWED_AUDIT_APPS = ("monetize",)


class MonetizationSurface(models.TextChoices):
    """Which monetization surface a flip touched — the audit table is ONE
    shared append-only trail for all of them, so the "who armed what, when"
    question is answerable in one place (Prompt 38: audit row when enabled)."""

    FLAG = "FLAG", "Feature flag"
    PLACEMENT = "PLACEMENT", "Promoted placement"
    TIER = "TIER", "Merchant tier"
    SACCO_TOOL = "SACCO_TOOL", "Sacco premium tool"


class AuditAction(models.TextChoices):
    """Persist enum .value, never the label (PROMPTS.md hard rule)."""

    ENABLED = "ENABLED", "Enabled"
    DISABLED = "DISABLED", "Disabled"


class PlacementSlot(models.TextChoices):
    """v1 carries exactly ONE slot kind — search-inline inside RESULTS
    (never the global /search/ hub, never the feed). The enum exists so a
    second slot is an additive value, not a schema change."""

    SEARCH_INLINE = "SEARCH_INLINE", "Search result inline slot"


class MerchantTier(models.TextChoices):
    """v1 scaffold keeps exactly two tiers (parent sketch: FREE/PRO) — the
    spec's 'local_pro'-style names can be introduced with billing, when the
    team proves demand. FREE is the state of any merchant without a row."""

    FREE = "FREE", "Free"
    PRO = "PRO", "Pro"


class PremiumTool(models.TextChoices):
    """V1-only tool stub: fleet analytics. Registration exists; DATA
    collection does not (see services.sacco_payload_doc)."""

    SESSION_ANALYTICS = "SESSION_ANALYTICS", "Session analytics"


class MonetizationAuditQuerySet(models.QuerySet):
    """WHY a guard at ORM level too (moderation's same WHY): accidental
    .update()/.delete() on audit rows must fail loudly, not silently
    rewrite evidence."""

    def update(self, *args, **kwargs):
        raise ValidationError("Audit rows are immutable (no bulk updates).")

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit rows are never deleted (evidence retention).")


class MonetizationAuditManager(models.Manager.from_queryset(MonetizationAuditQuerySet)):
    """Default manager: read-only surface. bulk_create stays possible (new rows)."""


class FeatureFlag(TimestampedModel):
    """A master monetization switch. Key/label per spec, label_sw completes
    the bilingual surface copy; enabled DEFAULTS False; the staff member who
    enabled it (enabler) and `enabled_at` stay NULL while off. These mirror
    columns are the CURRENT state; the append-only audit row is the HISTORY
    (both are written by the flip service — a row that lies about being on
    with nobody to blame is blocked in clean())."""

    key = models.CharField("Key", max_length=MAX_KEY_CHARS, unique=True)
    label = models.CharField("Label (en)", max_length=MAX_LABEL_CHARS)
    label_sw = models.CharField(
        "Label (sw)", max_length=MAX_LABEL_CHARS, blank=True, default=""
    )
    enabled = models.BooleanField("Enabled", default=False, db_index=True)
    enabler = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,  # display mirror; the audit actor is PROTECT
        null=True,
        blank=True,
        related_name="monetize_flags_enabled",
    )
    enabled_at = models.DateTimeField("Enabled at", null=True, blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        state = "on" if self.enabled else "off"
        return f"{self.key} ({state})"

    def clean(self):
        """The enable state must name its enabler — a flag cannot enable
        itself (nothing grants itself, Prompt 38)."""
        if self.enabled:
            if self.enabler_id is None:
                raise ValidationError(
                    {"enabler": "An enabled flag must record WHO unlocked it."}
                )
            if self.enabled_at is None:
                raise ValidationError(
                    {"enabled_at": "An enabled flag must record WHEN it was unlocked."}
                )


class MonetizationAudit(models.Model):
    """Append-only, per-flip audit row for EVERY monetization surface.

    Shape (spec): surface + action enum (ENABLED/DISABLED), actor, `at`
    (created_at), note — with the target as a generic FK restricted to the
    app's own four models (the moderation/disputes generic-FK idiom; its
    save-guard keeps the vocabulary local). Immutability mirrors
    moderation.ModeratorAudit: accidental .update()/.delete() fail loudly —
    corrections are NEW rows, never rewrites (evidence retention, KDPA).
    """

    surface = models.CharField(
        "Surface", max_length=16,
        choices=MonetizationSurface.choices, db_index=True,
    )
    action = models.CharField(
        "Action", max_length=16, choices=AuditAction.choices,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,  # no user deletion while audit rows exist
        null=True, blank=True,
        related_name="monetization_audits",
    )
    target_ct = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        limit_choices_to={"app_label__in": ALLOWED_AUDIT_APPS},
        related_name="+",
    )
    target_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("target_ct", "target_id")
    note = models.CharField("Note", max_length=MAX_NOTE_CHARS, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)  # the "at"

    objects = MonetizationAuditManager()  # read-only guard on update()/delete()

    class Meta:
        ordering = ["-created_at", "-id"]  # -id breaks same-second ties (stable renders)
        indexes = [
            models.Index(
                fields=["surface", "-created_at"], name="monaudit_surface_when",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.surface}.{self.action} by {self.actor} ({self.created_at:%Y-%m-%d})"

    def clean(self):
        if self.target_ct_id and self.target_ct.app_label not in ALLOWED_AUDIT_APPS:
            raise ValidationError(
                {"target": "Audit vocabulary stays inside apps.monetize."}
            )

    def save(self, *args, **kwargs):
        """The allow-list, enforced at the write path too (limit_choices_to
        only shapes forms; a raw .create() must still fail loudly)."""
        if self.target_ct_id and self.target_ct.app_label not in ALLOWED_AUDIT_APPS:
            raise ValidationError(
                {"target": "Audit vocabulary stays inside apps.monetize."}
            )
        super().save(*args, **kwargs)


class PromotedPlacement(TimestampedModel):
    """ONE listing's promotion into ONE slot kind. Defaults OFF (active
    False) with the bilingual disclosure stored on the row — the label copy
    ships INSIDE the payload a consumer renders (services.placement_payload);
    consumers render it, they never invent it. Enabling is a staff flip via
    services (+audit), never a seller self-serve.

    FK import safety: the listing points at the LISTINGS app's public model
    by string ("listings.Listing"); no import of listings internals — the
    read/write doors used are listings.services public functions only."""

    listing = models.ForeignKey(
        "listings.Listing",
        on_delete=models.CASCADE,
        related_name="promoted_placements",
    )
    slot = models.CharField(
        "Slot", max_length=16,
        choices=PlacementSlot.choices, default=PlacementSlot.SEARCH_INLINE.value,
        db_index=True,
    )
    active = models.BooleanField("Active", default=False, db_index=True)
    promoted_en = models.CharField(
        "Label (en)", max_length=MAX_LABEL_COPY_CHARS, default=PROMOTED_LABEL_EN,
    )
    promoted_sw = models.CharField(
        "Label (sw)", max_length=MAX_LABEL_COPY_CHARS, default=PROMOTED_LABEL_SW,
    )
    enabler = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="monetize_placements_enabled",
    )
    enabled_at = models.DateTimeField("Enabled at", null=True, blank=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "slot"], name="uniq_monetize_promoted_slot",
            ),
        ]

    def __str__(self) -> str:
        state = "on" if self.active else "off"
        return f"{self.listing_id} {self.slot} ({state})"

    def clean(self):
        if self.active:
            if self.enabler_id is None:
                raise ValidationError(
                    {"enabler": "An active placement must record WHO unlocked it."}
                )
            if self.enabled_at is None:
                raise ValidationError(
                    {"enabled_at": "An active placement must record WHEN it went live."}
                )


class MerchantEnrollment(TimestampedModel):
    """One merchant's tier row (user ↔ tier). The tier value is a FLAG, not
    a grant: services never let a merchant set it, only staff flips do (+
    audit on every real change). The FREE state needs no provenance (it is
    the default state of having a row — or of having none)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="merchant_tier",
    )
    tier = models.CharField(
        "Tier", max_length=8,
        choices=MerchantTier.choices, default=MerchantTier.FREE.value,
        db_index=True,
    )
    tier_set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="monetize_merchant_tiers_set",
    )
    tier_set_at = models.DateTimeField("Tier set at", null=True, blank=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self) -> str:
        return f"{self.user} → {self.tier}"

    def clean(self):
        if self.tier == MerchantTier.PRO.value:
            if self.tier_set_by_id is None:
                raise ValidationError(
                    {"tier_set_by": "A PRO tier must record WHO granted it."}
                )
            if self.tier_set_at is None:
                raise ValidationError(
                    {"tier_set_at": "A PRO tier must record WHEN it was granted."}
                )


class SaccoPremiumTool(TimestampedModel):
    """The SACCO premium-tools REGISTRY row (v1: SESSION_ANALYTICS is the
    only stub). enabled defaults False; FK import safety: points at the
    ROUTES app's Sacco by string — exactly what fleet's own models do —
    and the SACCO-admin gate reads fleet's PUBLIC service door.

    Bilingual tool copy lives on the row (label_en/label_sw) because tool
    labels are DATA here, not chrome."""

    sacco = models.ForeignKey(
        "routes.Sacco",
        on_delete=models.CASCADE,
        related_name="premium_tools",
    )
    tool = models.CharField(
        "Tool", max_length=24,
        choices=PremiumTool.choices, default=PremiumTool.SESSION_ANALYTICS.value,
    )
    label_en = models.CharField(
        "Label (en)", max_length=MAX_TOOL_LABEL_CHARS, default=SACCO_TOOL_LABEL_EN,
    )
    label_sw = models.CharField(
        "Label (sw)", max_length=MAX_TOOL_LABEL_CHARS, default=SACCO_TOOL_LABEL_SW,
    )
    enabled = models.BooleanField("Enabled", default=False, db_index=True)
    enabler = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="monetize_sacco_tools_enabled",
    )
    enabled_at = models.DateTimeField("Enabled at", null=True, blank=True)

    class Meta:
        ordering = ["sacco__name", "tool"]
        constraints = [
            models.UniqueConstraint(
                fields=["sacco", "tool"], name="uniq_monetize_sacco_tool",
            ),
        ]

    def __str__(self) -> str:
        state = "on" if self.enabled else "off"
        return f"{self.label_en} @ {self.sacco} ({state})"

    def clean(self):
        if self.enabled:
            if self.enabler_id is None:
                raise ValidationError(
                    {"enabler": "An enabled tool must record WHO switched it on."}
                )
            if self.enabled_at is None:
                raise ValidationError(
                    {"enabled_at": "An enabled tool must record WHEN it went live."}
                )
