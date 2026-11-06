"""Reviews models (Prompt 22) — DealReview: one held row per reviewer per deal.

Shape:
- DealReview   a 1–5 star + optional text review about the OTHER party of a
               completed deal. Deals are referenced EXTERNALLY (deal_app +
               deal_id — the generic app='barter-able' contract, see the
               module docstring in services) so this app never imports barter/
               services internals. The row is created HELD (ContentStatus
               .DRAFT) and becomes PUBLISHED ONLY through the public sweep
               (services.publish_due_reviews) — never on submit.

NON-RETALIATION (spec verbatim: "reviews publish simultaneously when both
submitted or after 14 days — no retaliation window"): submission creates a
HELD row only. The counterpart sees their own copy after submitting theirs;
the sweep publishes the pair in one tick, or unilaterally after 14 days, so
neither side can react to a rating that reached the public before theirs.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, TimestampedModel

# The generic deal-ref allow-list (origin apps whose deals can be reviewed).
# WHY a plain tuple of app_labels: barter (Prompt 21) and services (Prompt 18)
# are landing concurrently — reviews must never import them. Validation reads
# each origin's PUBLIC read door (services.py), a 3-line extension per app as
# deals become readable; everything here stays import-free.
ALLOWED_ORIGIN_APPS = ("listings", "services", "barter")

# Content bounds — one constant, one consumer, documented on purpose.
MAX_TEXT_CHARS = 500  # optional review text cap (mobile data budget + abuse)
MIN_DISPUTE_NOTE_CHARS = 20  # a 1-star explanation must actually explain
# Unilateral publication age (spec verbatim: "after 14 days"). Boundary is
# INCLUSIVE: a draft exactly 14 days old publishes on the next sweep.
UNILATERAL_PUBLISH_DAYS = 14

MSG_STAR_BOUNDS = _("Stars must be between 1 and 5.")
MSG_SELF_REVIEW = _(
    "You cannot review yourself — reviews are about the other party of a deal."
)
MSG_ALREADY_REVIEWED = _(
    "You have already reviewed this deal — one review per reviewer per deal."
)
MSG_DEAL_NOT_READABLE = _(
    "That deal does not exist or is not part of your deals to review."
)
MSG_ORIGIN_NOT_ALLOWED = _(
    "Reviews can only reference completed marketplace, service or barter deals."
)
MSG_DISPUTE_NOTE_REQUIRED = _(
    "A 1-star review must explain what went wrong (at least %(min)s "
    "characters) so moderators and the counterparty can see the reason."
) % {"min": MIN_DISPUTE_NOTE_CHARS}
MSG_NOT_HELD = _(
    "Reviews publish through the scheduled sweep only — publication time is "
    "never set by hand."
)
MSG_LOGIN = _("Log in to leave a review.")


def _disputes_app_landed() -> bool:
    """True once apps.disputes (Prompt 23) is importable. Gates only the
    CONDITION of the 1-star requirement, never the field itself."""
    try:
        import apps.disputes.services  # noqa: F401 — import IS the probe

        return True
    except ImportError:
        return False


def _dispute_is_open(deal_app: str, deal_id: int) -> bool:
    """Is a dispute OPEN on this deal? Prompt 23's public door contract:
    apps.disputes.services.dispute_open_for_deal(origin_app, origin_id)
    -> bool; until then the answer is honestly False (reviews created today
    still carry the 1-star note unconditionally — see clean())."""
    try:
        from apps.disputes import services as disputes_services

        door = getattr(disputes_services, "dispute_open_for_deal", None)
        if callable(door):
            return bool(door(deal_app, deal_id))
    except Exception:  # noqa: BLE001 — a broken dispute door cannot crash a review
        return False
    return False


def _dispute_note_required_for(stars: int, deal_app: str, deal_id: int) -> bool:
    """THE 1-star gate (single source of truth for model/service/form).

    Today (Prompt 22): every 1-star review requires the note — the disputes
    app does not exist yet, so "when the deal has a dispute open" would be
    permanently false and the gate would never fire. Once Prompt 23 lands
    the requirement narrows to deals with an OPEN dispute (spec verbatim),
    and only this condition changes. See the NOTE on models.DisputeReview.
    """
    if stars != 1:
        return False
    return not _disputes_app_landed() or _dispute_is_open(deal_app, deal_id)


class DealReview(TimestampedModel):
    """One party's review of the other on a referenced (external) deal.

    NOTE two-phase lifecycle: DRAFT rows are 'held' — invisible to the
    reviewed party and to reputation (no preview leak). The sweep promotes
    a pair together or a lone row at 14 days (injected clock). published_at
    is a dedicated column set BY THE SWEEP (auto_now_add-style stamps can't
    follow an injected clock — same reasoning as trust.BadgeEvent.awarded_at).
    """

    # --- the generic deal-ref contract (works for every origin app) -------
    deal_app = models.CharField(_("Deal origin app"), max_length=32, db_index=True)
    deal_id = models.PositiveBigIntegerField(_("Deal id"))

    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews_left",
        verbose_name=_("Reviewer"),
    )
    reviewed = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews_received",
        verbose_name=_("Reviewed member"),
    )

    stars = models.PositiveSmallIntegerField(_("Stars"))
    text = models.TextField(
        _("Review text"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_TEXT_CHARS)],
        help_text=_("Optional — at most %(n)s characters.") % {"n": MAX_TEXT_CHARS},
    )
    # NOTE (Prompt 23 tightening): today a 1-star review ALWAYS carries a
    # dispute note (stars==1 gate). When apps.disputes lands, the requirement
    # binds to "a dispute is OPEN on the referenced deal" (services.
    # _dispute_is_open reads that app's public door) — the field itself and
    # the minimum length stay.
    dispute_note = models.TextField(
        _("Dispute note (required for 1-star reviews)"),
        blank=True,
        default="",
        help_text=_(
            "Required for 1-star reviews: what was promised vs what happened."
        ),
    )

    status = models.CharField(
        _("State"),
        max_length=16,
        choices=ContentStatus.choices,
        default=ContentStatus.DRAFT.value,
        db_index=True,
        help_text=_("DRAFT (held) → PUBLISHED, by the sweep only."),
    )
    published_at = models.DateTimeField(_("Published at"), null=True, blank=True)

    class Meta:
        ordering = ["created_at", "pk"]  # the deterministic replay order
        constraints = [
            # One review per reviewer per deal — both-party pairs are two
            # rows; a reviewer never gets two chances to shape one deal's
            # reputation. Admin/BULK paths inherit it too (DB-level).
            models.UniqueConstraint(
                fields=["deal_app", "deal_id", "reviewer"],
                name="uniq_review_one_per_deal_reviewer",
            ),
            # reviewer ≠ reviewed as a DB guarantee (KDPA accountability:
            # no self-fanfare), belt-and-braces with model + service checks.
            models.CheckConstraint(
                condition=~Q(reviewer_id=F("reviewed_id")),
                name="no_self_deal_review",
            ),
            models.CheckConstraint(
                condition=Q(stars__gte=1) & Q(stars__lte=5),
                name="stars_between_1_and_5",
            ),
        ]
        indexes = [
            # Hot read paths: sweep by (status, deal) and rollup by (reviewed,
            # status). Deal id is positive — a compound index serves grouping.
            models.Index(
                fields=["status", "deal_app", "deal_id"], name="review_state_deal_idx"
            ),
            models.Index(
                fields=["reviewed", "status"], name="review_received_state_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.stars}★ {self.reviewer_id}→{self.reviewed_id} {self.deal_app}#{self.deal_id}"

    def clean(self):
        """The allow-list + bounds live on the MODEL so every write path
        (views, scripts, admin) inherits them (trust's precept)."""
        errors: dict[str, str] = {}
        if self.deal_app not in ALLOWED_ORIGIN_APPS:
            errors["deal_app"] = MSG_ORIGIN_NOT_ALLOWED
        if not (1 <= int(self.stars or 0) <= 5):
            errors["stars"] = MSG_STAR_BOUNDS
        if self.reviewer_id and self.reviewer_id == self.reviewed_id:
            errors["reviewed"] = MSG_SELF_REVIEW
        if self.status == ContentStatus.PUBLISHED.value and self.published_at is None:
            # Guard against hand-publication from any non-sweep surface.
            errors["published_at"] = MSG_NOT_HELD
        if int(self.stars or 0) == 1 and _dispute_note_required_for(
            int(self.stars or 0), self.deal_app, int(self.deal_id or 0)
        ) and len((self.dispute_note or "").strip()) < MIN_DISPUTE_NOTE_CHARS:
            errors["dispute_note"] = MSG_DISPUTE_NOTE_REQUIRED
        if errors:
            raise ValidationError(errors)
