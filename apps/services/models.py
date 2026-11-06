"""Service-listing models (Prompt 18) — skills marketplace.

SEPARATE from goods: this table is NOT a subset of apps.listings.Listing —
Prompt 18 says the model is a service listing with its own shape (rate
type + availability + portfolio). Goods live in apps.listings; a member
can hold BOTH; the two share vocabulary, never storage.

KDPA privacy contract baked into the shape of the model (same as listings):
- location is ONE public text field (`ward`) — ward/neighbourhood name ONLY.
  No lat/lon, no address column. The clean() guard refuses coordinate-shaped
  ward text; the regex is IMPORTED from apps.listings so both apps share one
  detector (drift here would be a privacy hole in one app).
- contact lives nowhere on this model — the "Message provider" link is
  messaging's enquiry origin (see the contract note in services.py).
- description / availability_notes are PLAIN TEXT. Templates render with
  |linebreaks on escaped text and NEVER |safe — user HTML cannot exist.
- NO review_flagged column: Prompt 18 carries no content-flag pitfall; a
  future scam/abuse flag goes through moderation's PUBLIC report_content
  when Prompt 22/23 wire a report affordance (one door, not per-app rules).

RATE-TYPE MATRIX (clean()-enforced, so admin/scripts/API inherit it):
  FIXED       — rate_amount REQUIRED
  HOURLY      — rate_amount REQUIRED
  NEGOTIABLE  — rate_amount FORBIDDEN (agreed in messages, like barter)

MERCHANTS' CONTRACT (Prompt 20 messaging lands in parallel — NOTE this is
the services-scoped twin of /chats/?listing=<pk>):
  enquiry link is the plain string f"/chats/?service={service.pk}" — messaging
  reads ?service=<pk> as its typed enquiry origin for SERVICE pk space
  (pk spaces of goods vs services are separate tables, so the param name
  tells messaging which table the pk belongs to).
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, ListingKind, TimestampedModel
# WHY the import (not a local copy): the KDPA coordinate guard is ONE shared
# contract with landed tests in apps/listings; identical regex everywhere.
from apps.listings.models import COORDINATE_RE, MSG_WARD_NOT_COORDINATES

# WHY a module-level tuple (apps.listings.LISTING_CATEGORIES pattern):
# Prompt 18 wants the brief's §3 Module 5 skill set as a seedable category
# list — extend by appending; rows store the plain .value string so adding
# a category needs no migration and old rows keep filtering correctly.
SERVICE_CATEGORIES = (
    ("tutoring", _("Tutoring")),
    ("repairs", _("Repairs")),
    ("coding", _("Coding")),
    ("tailoring", _("Tailoring")),
    ("catering", _("Catering")),
    ("photography", _("Photography")),
    ("farming", _("Farming services")),
)

CATEGORY_VALUES = {value for value, _label in SERVICE_CATEGORIES}


class RateType(models.TextChoices):
    """How a provider charges for the skill (Prompt 18: hourly / fixed /
    negotiable). Values frozen; labels translatable."""

    HOURLY = "HOURLY", _("Hourly rate")
    FIXED = "FIXED", _("Fixed price")
    NEGOTIABLE = "NEGOTIABLE", _("Negotiable")


# WHY 8, not 6: a portfolio must SHOW the skill (finished work, before/after)
# — one more than the goods cap by design, still a skim-friendly grid.
# Enforced in THREE places (model clean(), the upload endpoint, and the
# per-attachment clean()) — PROMPTS.md pitfall: client checks are decoration.
MAX_IMAGES = 8

MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 2000
MAX_WARD_CHARS = 80
MAX_AVAILABILITY_CHARS = 500

MSG_TOO_MANY_IMAGES = _("A service portfolio can hold up to %(n)d images.")
MSG_BAD_POSITION = _("Portfolio slot must be between 1 and %(n)d.")
MSG_POSITION_TAKEN = _("That portfolio slot is taken.")
MSG_DUPLICATE_PHOTO = _("That image is already in this portfolio.")

# The matrix, one message per broken shape — legible before the error shows.
MSG_RATE_AMOUNT_REQUIRED = _(
    "State the rate — hourly and fixed rates need an amount in KSh."
)
MSG_RATE_AMOUNT_FORBIDDEN = _(
    "Negotiable rates are agreed in your messages — clear the amount field."
)


class ServiceListing(TimestampedModel):
    """One service (skill) offered by a provider. Publish-on-create like
    listings/posts; moderation (Prompt 15) can layer UNDER_REVIEW later."""

    provider = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="service_listings",
    )
    # NOTE: stores the ListingKind.SERVICE .value always (pinned in clean()).
    # WHY a column at all on a services-only table: cross-module scans (feed,
    # Prompt 19 search, trust rollups) read ONE vocabulary — ListingKind —
    # from every marketplace row without knowing each app's internals.
    kind = models.CharField(
        _("Kind"),
        max_length=16,
        choices=ListingKind.choices,
        default=ListingKind.SERVICE.value,  # noqa: E501 — plain .value on purpose
        db_index=True,
    )
    title = models.CharField(
        _("Title"),
        max_length=MAX_TITLE_CHARS,
        validators=[MaxLengthValidator(MAX_TITLE_CHARS)],
    )
    title_sw = models.CharField(
        _("Title (Kiswahili)"),
        max_length=MAX_TITLE_CHARS,
        blank=True,
        default="",
    )
    description = models.TextField(
        _("Description"),
        max_length=MAX_DESC_CHARS,
        validators=[MaxLengthValidator(MAX_DESC_CHARS)],
    )
    description_sw = models.TextField(
        _("Description (Kiswahili)"),
        max_length=MAX_DESC_CHARS,
        blank=True,
        default="",
    )
    # NOTE: stores the SERVICE_CATEGORIES .value ("tutoring" etc.) — never
    # the translated label (hard rule: labels change with locale).
    category = models.CharField(
        _("Category"), max_length=32, choices=SERVICE_CATEGORIES, db_index=True
    )
    # WHY the default is NEGOTIABLE: the only rate-type that is valid with
    # NO amount, so a bare/programmatic row survives full_clean(); the form
    # still asks explicitly, and FIXED/HOURLY rows must carry the amount.
    rate_type = models.CharField(
        _("Rate type"),
        max_length=16,
        choices=RateType.choices,
        default=RateType.NEGOTIABLE,
        db_index=True,
    )
    # WHY nullable: negotiable services have no stated number — the pair
    # renders as EITHER "KSh 800 / hour" OR "Negotiable", never both.
    rate_amount = models.DecimalField(
        _("Rate (KSh)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    # Availability is a NOTE, not a schedule — honest V1 scope (no bookings
    # module exists); plain text, ≤500 chars.
    availability_notes = models.TextField(
        _("Availability notes"),
        max_length=MAX_AVAILABILITY_CHARS,
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_AVAILABILITY_CHARS)],
    )
    # THE public location field — ward/neighbourhood name ONLY (module
    # docstring). Same coordinate-refusing clean() as listings.
    ward = models.CharField(
        _("Ward / neighbourhood"),
        max_length=MAX_WARD_CHARS,
        db_index=True,
    )
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=ContentStatus.choices,
        default=ContentStatus.DRAFT,
        db_index=True,
    )
    images = models.ManyToManyField(
        "media.MediaRef",
        through="ServiceImage",
        related_name="service_listings",
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title[:40]} ({self.ward})"

    def clean(self):
        """Model-level rules that every write path inherits (admin, API,
        scripts — view/form checks are decoration). Image cap is pk-gated:
        attachment rows cannot exist pre-save; the per-attach gate lives in
        ServiceImage.clean()."""
        # Pin the vocabulary: no write path can store a non-SERVICE kind.
        self.kind = ListingKind.SERVICE.value
        errors = {}
        # THE rate-type matrix (see module docstring).
        if self.rate_type in (RateType.HOURLY, RateType.FIXED):
            if self.rate_amount is None:
                errors["rate_amount"] = [MSG_RATE_AMOUNT_REQUIRED]
        elif self.rate_type == RateType.NEGOTIABLE:
            if self.rate_amount is not None:
                errors["rate_amount"] = [MSG_RATE_AMOUNT_FORBIDDEN]
        # Coordinate-shaped ward text = precise location → refused, same as
        # listings (one regex, one message — imported above).
        if self.ward and COORDINATE_RE.search(self.ward):
            errors["ward"] = [
                MSG_WARD_NOT_COORDINATES,
            ]
        if self.pk and self.attachments.count() > MAX_IMAGES:
            # WHY "__all__" (NON_FIELD_ERRORS): "images" is not a form field
            # on ServiceForm; a non-field error lands on the form without
            # the unknown-key ValueError trap.
            errors.setdefault(NON_FIELD_ERRORS, []).append(
                MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}
            )
        if errors:
            raise ValidationError(errors)


class ServiceImage(TimestampedModel):
    """One ordered portfolio slot (position 1..8). Same bridge shape as
    ListingImage: stable ordering + a per-attachment home for the cap."""

    service = models.ForeignKey(
        ServiceListing, on_delete=models.CASCADE, related_name="attachments"
    )
    image = models.ForeignKey(
        "media.MediaRef",
        on_delete=models.CASCADE,
        related_name="service_images",
    )
    position = models.PositiveSmallIntegerField(_("Position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["service", "position"], name="uniq_service_position"
            ),
            # One image once per portfolio — no accidental double-attach.
            models.UniqueConstraint(
                fields=["service", "image"], name="uniq_service_image"
            ),
        ]

    def __str__(self) -> str:
        return f"service {self.service_id} #{self.position}"

    def clean(self):
        if not (1 <= self.position <= MAX_IMAGES):
            raise ValidationError(
                {"position": MSG_BAD_POSITION % {"n": MAX_IMAGES}}
            )
        if not self.service_id:
            return  # foreign-key integrity errors will catch this first
        siblings = ServiceImage.objects.filter(service_id=self.service_id)
        if self.pk:
            siblings = siblings.exclude(pk=self.pk)
        if siblings.filter(position=self.position).exists():
            raise ValidationError({"position": MSG_POSITION_TAKEN})
        if self.image_id and siblings.filter(image_id=self.image_id).exists():
            raise ValidationError({"image": MSG_DUPLICATE_PHOTO})
        # THE per-attachment cap layer — a simultaneous write that raced past
        # the endpoint's pre-check still fails here before it lands.
        if not self.pk and siblings.count() >= MAX_IMAGES:
            raise ValidationError(
                {"image": MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}}
            )
