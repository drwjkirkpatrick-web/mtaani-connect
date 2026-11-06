"""Listing models (Prompt 17) — marketplace goods listings.

KDPA privacy contract baked into the shape of the model:
- location is ONE public text field (`ward`) — ward/neighbourhood name ONLY.
  There is deliberately NO lat/lon, NO address field, NO point column. The
  clean() guard additionally refuses coordinate-shaped strings smuggled into
  the ward text (tests cover "exposure" by rendering real pages).
- contact lives nowhere on this model — no phone field, no contact_name.
  In-app messaging (Prompt 20) is the ONLY contact channel; the detail page
  renders a "Message seller" link documented in note (see contract below).
- description is PLAIN TEXT. Templates render it with |linebreaks (fixed
  allow-list transform on escaped text) and NEVER |safe — user HTML cannot
  exist anywhere in listing content (same rule as posts/guides).

WHY the phone-pattern flag is a FIELD, not a status: Prompt 17's pitfall says
numbers in descriptions go to the moderation QUEUE, not auto-block — people
legitimately discuss phone-shaped things. `review_flagged` records that a
detector matched; humans decide what happens next.

MERCHANTS' CONTRACT via prompt-sibling consumer (Prompt 20 messaging):
  enquiry link is the plain string f"/chats/?listing={listing.pk}" — messaging
  reads the ?listing= query param as its enquiry origin (typed reference to
  the listing). Kept a plain string on purpose: messaging's app is landing in
  parallel and its concrete route name is not stable yet.
"""
from decimal import Decimal
import re

from django.conf import settings
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, TimestampedModel

# WHY a module-level tuple (same pattern as GUIDE_CATEGORIES in apps/guides):
# Prompt 17 wants a "seedable category list" — extend by appending
# (value, label); rows store the plain .value string, so adding a category
# needs no migration and old rows keep filtering correctly.
LISTING_CATEGORIES = (
    ("new", _("New / sealed")),
    ("second-hand", _("Second-hand")),
    ("handmade", _("Handmade")),
    ("farm produce", _("Farm produce")),
    ("electronics", _("Electronics")),
    ("furniture", _("Furniture")),
    ("tools", _("Tools")),
)

CATEGORY_VALUES = {value for value, _label in LISTING_CATEGORIES}

# NOTE: listings-local vocabulary (not shared state) — the physical condition
# grade a seller self-reports. Values are frozen; labels are translatable.
class Condition(models.TextChoices):
    """Physical condition grade self-reported by the seller (Prompt 17)."""

    NEW = "new", _("Brand new")
    LIKENEW = "likenew", _("Like new")
    GOOD = "good", _("Good")
    FAIR = "fair", _("Fair")
    WORN = "worn", _("Worn")


# WHY 6, not 4: goods listings want the whole item visible (all sides); the
# cap is enforced in THREE places (model clean(), the upload endpoint, and
# the per-attachment clean() — PROMPTS.md pitfall: client checks are
# decoration). All three import this constant; nobody re-types the number.
MAX_IMAGES = 6

MAX_TITLE_CHARS = 120
MAX_DESC_CHARS = 2000
MAX_WARD_CHARS = 80

MSG_TOO_MANY_IMAGES = _("A listing can hold up to %(n)d photos.")
MSG_BAD_POSITION = _("Photo slot must be between 1 and %(n)d.")
MSG_POSITION_TAKEN = _("That photo slot is taken.")
MSG_DUPLICATE_PHOTO = _("That photo is already on this listing.")

MSG_PRICE_OR_NEGOTIABLE = _(
    "Give a price or mark the listing negotiable — one of the two is required."
)
# WHY the regex (not a form hint): a "ward" containing a decimal number like
# "-1.2921, 36.817223" is GPS precision — KDPA forbids precise coordinates or
# exact addresses in public fields. Integers are fine ("Plot 45", "Stage 3").
COORDINATE_RE = re.compile(r"-?\d{1,3}\.\d{2,}")
MSG_WARD_NOT_COORDINATES = _(
    "Share only the ward or neighbourhood name — never coordinates or an "
    "exact address (this is a public page)."
)


class Listing(TimestampedModel):
    """One goods listing. Status reuses ContentStatus; publishing is instant
    on create for members (same one-tap flow as posts), so marketplaces feel
    alive; moderation (Prompt 15) layers UNDER_REVIEW on flagged rows."""

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="listings",
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
    # NOTE: stores the LISTING_CATEGORIES .value ("farm produce" etc.) —
    # never the translated label (hard rule: labels change with locale).
    category = models.CharField(
        _("Category"), max_length=32, choices=LISTING_CATEGORIES, db_index=True
    )
    condition = models.CharField(
        _("Condition"),
        max_length=16,
        choices=Condition.choices,
        default=Condition.GOOD,
    )
    # WHY nullable + separate flag: "negotiable" IS the Kikapu handshake —
    # the pair renders as either "KSh 1,500" or "Negotiable", never both.
    price = models.DecimalField(
        _("Price (KSh)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    price_negotiable = models.BooleanField(_("Price negotiable"), default=False)
    # THE public location field. Ward/neighbourhood name ONLY — see module
    # docstring. Guarded by clean() against coordinate-shaped strings.
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
    # WHY a boolean flag (not a status): the DETECTOR's finding ("a
    # phone-shaped string is in the public text") is orthogonal to the
    # lifecycle (PUBLISHED still). Humans in the moderation queue decide.
    review_flagged = models.BooleanField(
        _("Flagged for review"), default=False, db_index=True
    )
    images = models.ManyToManyField(
        "media.MediaRef", through="ListingImage", related_name="listings", blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title[:40]} ({self.ward})"

    def clean(self):
        """Model-level rules that every write path inherits (admin, API,
        scripts — client-side and view-side checks are decoration).
        Image cap is pk-gated: attachment rows cannot exist pre-save; the
        per-attach gate lives in ListingImage.clean()."""
        errors = {}
        if not (self.price is not None or self.price_negotiable):
            errors["price"] = [
                MSG_PRICE_OR_NEGOTIABLE,
            ]
        # Coordinate-shaped ward text = precise location → refuse here so
        # even scripts/admin paths cannot smuggle GPS precision in.
        if self.ward and COORDINATE_RE.search(self.ward):
            errors["ward"] = [
                MSG_WARD_NOT_COORDINATES,
            ]
        if self.pk and self.attachments.count() > MAX_IMAGES:
            # WHY the "__all__" key (NON_FIELD_ERRORS): "images" is not a
            # form field on ListingForm; a non-field error lands on the form
            # without the unknown-key ValueError trap.
            errors.setdefault(NON_FIELD_ERRORS, []).append(
                MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}
            )
        if errors:
            raise ValidationError(errors)


class ListingImage(TimestampedModel):
    """One ordered photo slot of a listing (position 1..6). Same bridge shape
    as PostImage: ordering must be stable and the cap needs a per-attachment
    home — a through model instead of bare M2M."""

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name="attachments"
    )
    image = models.ForeignKey(
        "media.MediaRef", on_delete=models.CASCADE, related_name="listing_images"
    )
    position = models.PositiveSmallIntegerField(_("Position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "position"], name="uniq_listing_position"
            ),
            # One photo once per listing — no accidental double-attach.
            models.UniqueConstraint(
                fields=["listing", "image"], name="uniq_listing_image"
            ),
        ]

    def __str__(self) -> str:
        return f"listing {self.listing_id} #{self.position}"

    def clean(self):
        if not (1 <= self.position <= MAX_IMAGES):
            raise ValidationError(
                {"position": MSG_BAD_POSITION % {"n": MAX_IMAGES}}
            )
        if not self.listing_id:
            return  # foreign-key integrity errors will catch this first
        siblings = ListingImage.objects.filter(listing_id=self.listing_id)
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
