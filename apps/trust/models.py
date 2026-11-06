"""Trust models (Prompt 16) — helpful votes, tier thresholds, badge events.

Shape:
- HelpfulVote  one row per (voter, target) via generic FK LIMITED to the
               allow-list apps ['posts', 'qa']. Row existence IS the vote —
               no counters to drift (same choice as qa.AnswerVote). Votes are
               REVOCABLE: services.remove_vote deletes the row.
- TrustTier    ADMIN-TUNABLE threshold rows: category × level →
               (min_lifetime, min_90d). Seeded by data migration 0002 with
               sensible numbers; admins tune them in the admin WITHOUT code
               changes. The structure carries NO hardcoded persona content —
               "who is an expert" is only ever data.
- BadgeEvent   one row per (user, category, level) — UNIQUE, so re-crossing a
               threshold never refires. Immutable award record (badges are
               earned-once lifetime achievements; falling below a tier later
               does not delete the event).

Why per CATEGORY: a helpful member accumulates trust in the domain they help
in (e.g. medical answers), not one giant global score. Where the category
comes from is services.category_of (public mapping, posts → GENERAL).
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel

# Spec verbatim: targets are posts and answers ONLY. Stored as app labels so
# future target types are additions to THIS tuple, never code changes in
# service logic.
ALLOWED_TARGET_APPS = ("posts", "qa")

MSG_TARGET_NOT_ALLOWED = _(
    "Only posts and answers can receive helpful votes."
)
MSG_TARGET_REMOVED = _(
    "You cannot vote on content that has been removed."
)
MSG_SELF_VOTE = _(
    "You cannot mark your own content as helpful — wait for the community."
)
MSG_TARGET_MISSING = _("That content no longer exists.")


class TrustLevel(models.TextChoices):
    """Expert-tag levels (spec: rising / known / trusted).

    NOTE: values are the frozen vocabulary RISING/KNOWN/TRUSTED (repo enum
    convention, uppercase like SafetyCategory); the spec text writes them
    lowercase. Labels are translatable — store .value, never the label.
    """

    RISING = "RISING", _("Rising helper")
    KNOWN = "KNOWN", _("Known helper")
    TRUSTED = "TRUSTED", _("Trusted helper")


LEVEL_RANK = {  # highest held tier = max rank; deterministic order everywhere
    TrustLevel.RISING.value: 0,
    TrustLevel.KNOWN.value: 1,
    TrustLevel.TRUSTED.value: 2,
}

# Trust vocabulary for the GENERAL bucket. Posts have no category field of
# their own (only the educational flag, which is posts'/feed's business) —
# they roll into GENERAL. Answers roll into their snapshot safety_category,
# with "NONE" meaning everyday knowledge →GENERAL as well. See services.
GENERAL_CATEGORY = "GENERAL"


class HelpfulVote(TimestampedModel):
    """"I found this helpful" — one row per member per post/answer."""

    voter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="helpful_votes", verbose_name=_("Voter"),
    )
    # WHY a written-at-vote-time owner FK: the HELPFUL person is the content
    # author — helpful votes accrue to WHO the content helps, and the rollup
    # ("votes RECEIVED") must stay a single-table aggregation on this app's
    # own rows, pinning the recipient exactly like the category (qa's own
    # precedent: answers pin safety_category so later edits never re-define
    # what a past vote helped).
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="trust_received_votes",
        verbose_name=_("Content author (helpfulness recipient)"),
    )
    # WHY a written-at-vote-time category column: rollups become a pure
    # single-table aggregation — no cross-app table reads at read time. This
    # mirrors qa's own precedent (answers PIN safety_category at answer time
    # so later edits never re-define what a past vote helped), and it keeps
    # the row honest about what the content was WHEN it was voted helpful.
    category = models.CharField(_("Category"), max_length=32, db_index=True)
    target_ct = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="trust_votes",
    )
    target_id = models.PositiveBigIntegerField(_("Target id"))
    target = GenericForeignKey("target_ct", "target_id")

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # One vote one member one item; idempotent votes are enforced by
            # the DB itself, so bulk/admin/script paths cannot stack votes.
            models.UniqueConstraint(
                fields=["voter", "target_ct", "target_id"],
                name="uniq_trust_vote_per_user_target",
            ),
        ]
        indexes = [
            models.Index(
                fields=["target_ct", "target_id"], name="trust_target_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.voter_id} → {self.target_ct.app_label}#{self.target_id}"

    def clean(self):
        # The allow-list lives on the MODEL (qa/guides pitfall pattern): any
        # write path (admin, script, future API) inherits it automatically.
        try:
            app_label = self.target_ct.app_label
        except (ContentType.DoesNotExist, AttributeError):
            raise ValidationError({"target": MSG_TARGET_MISSING})
        if app_label not in ALLOWED_TARGET_APPS:
            raise ValidationError({"target": MSG_TARGET_NOT_ALLOWED})


class TrustTier(TimestampedModel):
    """An admin-tunable award threshold.

    WHY DB rows and not code: "admin-tunable thresholds, no hardcoded persona
    content" (PROMPTS.md 16). Changing who can be called 'trusted' is an
    admin edit, never a deploy. Rows are seeded per category by migration
    0002; admins may add rows for any category value.
    """

    category = models.CharField(
        _("Category"), max_length=32, db_index=True,
        help_text=_(
            "Content category the tier applies to (e.g. 'GENERAL', or a "
            "safety category such as 'MEDICAL')."
        ),
    )
    level = models.CharField(_("Level"), max_length=16, choices=TrustLevel.choices)
    min_lifetime = models.PositiveIntegerField(
        _("Min lifetime helpful votes"),
        help_text=_("Lifetime helpful-vote count needed to hold this level."),
    )
    min_90d = models.PositiveIntegerField(
        _("Min last-90-days helpful votes"),
        help_text=_(
            "Helpful votes received in the last 90 days needed to hold this "
            "level (keeps badges for TODAY'S helpers, not faded glory)."
        ),
    )

    class Meta:
        ordering = ["category", "min_lifetime", "min_90d", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "level"], name="uniq_trust_tier_category_level",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.category}/{self.level} ≥{self.min_lifetime}L/{self.min_90d}D"


class BadgeEvent(TimestampedModel):
    """A threshold crossing, one row per (user, category, level) — UNIQUE,
    so crossing again (or falling and re-crossing) never refires an award.
    This is the durable provenance of an expert tag (Prompt 22 reads the
    history; profile surfaces read services.helpfulness_badges)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="trust_badge_events",
    )
    category = models.CharField(_("Category"), max_length=32)
    level = models.CharField(_("Level"), max_length=16, choices=TrustLevel.choices)
    # WHY a dedicated column (created_at already exists): awarded_at stamps
    # the EVALUATION clock passed in by the service (injectable for tests);
    # auto_now_add bookkeeping stamps cannot follow an injected clock.
    awarded_at = models.DateTimeField(_("Awarded at"), db_index=True)

    class Meta:
        ordering = ["awarded_at", "category", "level", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "category", "level"],
                name="uniq_trust_badge_once",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "category"], name="trust_badge_user_cat"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.category}/{self.level}"
