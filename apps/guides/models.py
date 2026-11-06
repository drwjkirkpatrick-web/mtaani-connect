"""Guide models (Prompt 08) — "WikiHow for Kenya": practical community how-tos.

Shape (documented for Prompt 09, which hashes revisions for offline sync):
- Guide              identity + CURRENT working copy (what the edit form opens)
- GuideRevision      a frozen snapshot created on EVERY edit; only revisions
                     with status PUBLISHED are shown publicly ("latest
                     approved revision")
- GuideStep          ordered rows of ONE revision — steps are revision-scoped
                     so an edit can never rewrite an already-published page
- GuideConfirmation  "This worked for me in [ward]" — ward is a plain CharField
                     text label (KDPA: coarse location only, never phones or
                     coordinates; profiles stores ward the same way)

Revision/approval flow NOTE (simple, per Prompt 08): a revision is born
DRAFT; publishing sets it PUBLISHED (the author approves their own work;
UNDER_REVIEW exists for Prompt 15 moderation). The detail view shows the
newest revision with status PUBLISHED. Full flow lives in services.py.

PITFALL GUARD: step text is PLAIN TEXT. Templates render it with the fixed
|linebreaks transform and NEVER with |safe — user HTML cannot exist anywhere
in guide content.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory, TimestampedModel

# WHY a module-level tuple: Prompt 08 wants a "seedable, admin-extendable"
# list — extend by appending (value, label) here; values are stored as plain
# .value strings, so no migration is needed when new categories are added.
GUIDE_CATEGORIES = (
    ("cooking", _("Cooking")),
    ("farming", _("Farming")),
    ("repairs", _("Repairs")),
    ("business", _("Business")),
    ("health-education", _("Health education")),
    ("crafts", _("Crafts")),
    ("transport", _("Transport")),
)

SKILL_LEVELS = (
    ("beginner", _("Beginner")),
    ("intermediate", _("Intermediate")),
    ("advanced", _("Advanced")),
)

# The safety categories that REQUIRE a safety_warning (PROMPTS.md Prompt 08).
# Stored as raw .value strings — never labels (labels are translatable).
RISKY_SAFETY_CATEGORIES = (
    SafetyCategory.MEDICAL.value,
    SafetyCategory.ELECTRICAL.value,
    SafetyCategory.CONSTRUCTION.value,
)

# WHY a cap: a guide page must stay inside the 50 KB mobile budget; runaway
# step text is the cheapest way to blow it. Enforced in full_clean().
MAX_STEP_CHARS = 2000


def _safety_warning_missing(*, safety_category: str, safety_warning: str) -> bool:
    return safety_category in RISKY_SAFETY_CATEGORIES and not safety_warning.strip()


class Guide(TimestampedModel):
    """The container + current working copy. Displayed content always comes
    from a GuideRevision, never from here (revision safety)."""

    title_en = models.CharField(_("Title (English)"), max_length=200)
    title_sw = models.CharField(
        _("Title (Kiswahili)"), max_length=200, blank=True, default=""
    )
    category = models.CharField(_("Category"), max_length=32, choices=GUIDE_CATEGORIES, db_index=True)
    skill_level = models.CharField(_("Skill level"), max_length=16, choices=SKILL_LEVELS)
    safety_category = models.CharField(
        _("Safety handling"),
        max_length=16,
        choices=SafetyCategory.choices,
        default=SafetyCategory.NONE,
    )
    safety_warning = models.TextField(_("Safety warning"), blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="guides"
    )
    status = models.CharField(
        _("Status"), max_length=16, choices=ContentStatus.choices,
        default=ContentStatus.DRAFT, db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.display_title

    @property
    def display_title(self) -> str:
        """Kiswahili title when present, English otherwise (i18n fallback)."""
        return self.title_sw or self.title_en

    @property
    def title_is_fallback(self) -> bool:
        """True when the English title is shown because no Kiswahili title
        exists yet — the view labels this visibly (Prompt 08 test contract)."""
        return not self.title_sw

    def clean(self):
        # The gate lives on the MODEL, not just the form (PROMPTS.md pitfall):
        # any future write path (admin, API, script) inherits it automatically.
        if _safety_warning_missing(
            safety_category=self.safety_category, safety_warning=self.safety_warning
        ):
            raise ValidationError(
                {
                    "safety_warning": _(
                        "A safety warning is required for medical, electrical "
                        "or construction guides."
                    )
                }
            )


class GuideRevision(TimestampedModel):
    """One immutable snapshot of a guide's content. Every edit creates a new
    row (version = max+1). Publishing is what makes a version public."""

    guide = models.ForeignKey(Guide, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField(_("Version"))
    title_en = models.CharField(_("Title (English)"), max_length=200)
    title_sw = models.CharField(
        _("Title (Kiswahili)"), max_length=200, blank=True, default=""
    )
    safety_category = models.CharField(
        _("Safety handling"), max_length=16, choices=SafetyCategory.choices,
        default=SafetyCategory.NONE,
    )
    safety_warning = models.TextField(_("Safety warning"), blank=True, default="")
    status = models.CharField(
        _("Status"), max_length=16, choices=ContentStatus.choices,
        default=ContentStatus.DRAFT, db_index=True,
    )
    published_at = models.DateTimeField(_("Published at"), null=True, blank=True)

    class Meta:
        ordering = ["version"]
        constraints = [
            models.UniqueConstraint(
                fields=["guide", "version"], name="uniq_guide_version"
            )
        ]

    def __str__(self) -> str:
        return f"v{self.version} — {self.display_title}"

    @property
    def display_title(self) -> str:
        return self.title_sw or self.title_en

    @property
    def title_is_fallback(self) -> bool:
        return not self.title_sw

    def clean(self):
        # WHY here too: the snapshot is what actually gets displayed, so the
        # safety rule must hold per revision, not only on the working copy.
        if _safety_warning_missing(
            safety_category=self.safety_category, safety_warning=self.safety_warning
        ):
            raise ValidationError(
                {
                    "safety_warning": _(
                        "A safety warning is required for medical, electrical "
                        "or construction guides."
                    )
                }
            )


class GuideStep(TimestampedModel):
    """An ordered instruction belonging to ONE revision (snapshot-scoped).
    text is PLAIN TEXT: it holds no HTML and templates must never mark it
    |safe — line breaks are added with the Django |linebreaks filter."""

    revision = models.ForeignKey(
        GuideRevision, on_delete=models.CASCADE, related_name="steps"
    )
    position = models.PositiveIntegerField(_("Position"), db_index=True)
    # WHY blank=True: the step builder renders optional skip rows; only the
    # SERVICE persists steps (whitespace-only entries are dropped there), so
    # a blank text value is a builder artifact — never persisted data.
    text = models.TextField(
        _("What to do"),
        blank=True,
        default="",
        max_length=MAX_STEP_CHARS,
        validators=[MaxLengthValidator(MAX_STEP_CHARS)],
    )
    # Prompt 06 pipeline: files flow through media.save_upload → MediaRef
    # (EXIF stripped, WebP variants). We keep only the ref, never the file.
    image = models.ForeignKey(
        "media.MediaRef", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="guide_steps",
    )

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "position"], name="uniq_step_position"
            )
        ]

    def __str__(self) -> str:
        return f"{self.revision_id}: {self.position}. {self.text[:40]}"

    def clean(self):
        if self.position < 1:
            raise ValidationError({"position": _("Steps start at position 1.")})


class GuideConfirmation(TimestampedModel):
    """'This worked for me in [ward]' — the community verification signal.

    KDPA note: ward is intentionally a coarse plain-text label, exactly like
    Profile.ward — no coordinates, no phone numbers, nothing precise.
    One confirmation per member per guide; confirming again updates it.
    """

    guide = models.ForeignKey(
        Guide, on_delete=models.CASCADE, related_name="confirmations"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="guide_confirmations",
    )
    ward = models.CharField(_("Ward where this worked"), max_length=64)
    note = models.TextField(_("Details"), blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["guide", "user"], name="uniq_confirmation_per_user"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} worked this in {self.ward}"
