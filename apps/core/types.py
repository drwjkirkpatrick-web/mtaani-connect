"""Core shared vocabulary (PROMPTS.md Prompt 02).

WHY one types module: every app imports these enums so state names can
never drift between modules ("CANCELLED" vs "canceled" vs "closed").

HARD RULE (PROMPTS.md): persist enum `.value`, never `.label` — labels
are translatable and WILL change (en + sw). Values are frozen vocabulary.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _


class Confidence(models.TextChoices):
    """Source-of-truth label for transport data.

    WHY NO_DATA exists: the app never fabricates positions, ETAs or fares
    (product brief §9). Absence of data is a first-class, displayable state.
    """

    LIVE_GPS = "LIVE_GPS", _("Live GPS")
    VEHICLE_REPORT = "VEHICLE_REPORT", _("Vehicle report")
    COMMUNITY_REPORT = "COMMUNITY_REPORT", _("Community report")
    ESTIMATE = "ESTIMATE", _("Estimate")
    NO_DATA = "NO_DATA", _("No data")


class LocationVisibility(models.TextChoices):
    """Coarse-by-default location sharing for profiles (KDPA minimization)."""

    WARD = "WARD", _("Ward only")
    NEIGHBOURHOOD = "NEIGHBOURHOOD", _("Neighbourhood")
    CITY = "CITY", _("City")


class BarterMode(models.TextChoices):
    """Deal composition (product brief §3 Module 6)."""

    CASH = "CASH", _("Cash only")
    BARTER = "BARTER", _("Barter only")
    CASH_PLUS = "CASH_PLUS", _("Cash plus barter")


class OfferStatus(models.TextChoices):
    """Barter offer state machine — transitions validated in apps/barter (21)."""

    DRAFT = "DRAFT", _("Draft")
    OFFERED = "OFFERED", _("Offered")
    COUNTERED = "COUNTERED", _("Countered")
    ACCEPTED = "ACCEPTED", _("Accepted")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Expired")


class ReportReason(models.TextChoices):
    """Why content was reported (moderation, Prompt 15)."""

    SPAM = "SPAM", _("Spam")
    SCAM = "SCAM", _("Scam")
    HARASSMENT = "HARASSMENT", _("Harassment")
    MISINFORMATION = "MISINFORMATION", _("Misinformation")
    OFF_TOPIC = "OFF_TOPIC", _("Off topic")
    SAFETY = "SAFETY", _("Safety")


class SafetyCategory(models.TextChoices):
    """Guides/Q&A safety handling (PROMPTS.md Prompts 08, 12)."""

    MEDICAL = "MEDICAL", _("Medical")
    ELECTRICAL = "ELECTRICAL", _("Electrical")
    CONSTRUCTION = "CONSTRUCTION", _("Construction")
    NONE = "NONE", _("No special handling")


class ContentStatus(models.TextChoices):
    """Content lifecycle shared by posts, guides, listings, messages."""

    DRAFT = "DRAFT", _("Draft")
    PUBLISHED = "PUBLISHED", _("Published")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    REMOVED = "REMOVED", _("Removed")


class ListingKind(models.TextChoices):
    """Marketplace top-level split: goods vs skills/services."""

    GOODS = "GOODS", _("Goods")
    SERVICE = "SERVICE", _("Service")


class GroupKind(models.TextChoices):
    """Group taxonomy (product brief §3 Module 4)."""

    INTEREST = "INTEREST", _("Interest")
    NEIGHBOURHOOD = "NEIGHBOURHOOD", _("Neighbourhood")
    PROFESSION = "PROFESSION", _("Profession")


class TimestampedModel(models.Model):
    """Abstract base: UTC created/updated stamps on every content model.

    NOTE: storage is UTC (USE_TZ=True); Africa/Nairobi is display-only.
    """

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
