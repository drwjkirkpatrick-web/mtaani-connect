"""Post models (Prompt 10) — word-of-mouth posts with photos + typed refs.

Shape (documented for Prompt 11, which consumes the read API in services.py):
- Post        text (plain, ≤2,000 chars) + educational tag + typed reference
- PostImage   the ordered bridge post ↔ MediaRef (1..4 positions). M2M through
              a through model instead of bare M2M: ordering must be stable and
              the cap check needs a place to live per-attachment.

Two references, TYPED as separate FK-style fields — never a raw URL charfield
(PROMPTS.md pitfall: preview cards render server-side; a URL string would be
an XSS surface and an unpreviewable blob):
- ref_guide    apps.guides.Guide pk (FK); preview reads the guides app's
               PUBLIC query functions (visible_guides, latest_published_revision)
- ref_listing  listing pk as an integer field (Prompt 17 does not exist yet).
               NO_DATA honesty rule: the preview renders "Listing preview
               coming soon" — we never invent listing data it does not have.

PITFALL GUARD: post text is PLAIN TEXT. Templates render it with the fixed
|linebreaks transform and NEVER with |safe — user HTML cannot exist anywhere
in post content (same rule as guides).
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, TimestampedModel

# WHY 2,000: word-of-mouth posts are short by culture ("short-form first
# class, not truncated long-form") — the cap is generous but bounded, and
# chars_remaining powers the form hint + tests.
MAX_POST_CHARS = 2000

# WHY a module-level constant: the cap is enforced in TWO places (model
# clean() AND the upload endpoint — PROMPTS.md pitfall; client-side checks
# are decoration). Both must import this, never re-type the number.
MAX_IMAGES = 4

MSG_TOO_MANY_IMAGES = _("A post can hold up to %(n)d photos.")
MSG_BAD_POSITION = _("Photo slot must be between 1 and %(n)d.")
MSG_POSITION_TAKEN = _("That photo slot is taken.")
MSG_DUPLICATE_PHOTO = _("That photo is already on this post.")


class Post(TimestampedModel):
    """One community post. Publishing is instant on create for members
    (word-of-mouth flow); moderation (Prompt 15) layers UNDER_REVIEW later."""

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="posts"
    )
    text = models.TextField(
        _("What's happening"),
        max_length=MAX_POST_CHARS,
        validators=[MaxLengthValidator(MAX_POST_CHARS)],
    )
    # WHY a plain book on the post: the educational tag is filterable
    # (Prompt 10 contract) and a ranking signal for the Prompt 11 feed.
    educational = models.BooleanField(_("Educational"), default=False)
    status = models.CharField(
        _("Status"), max_length=16, choices=ContentStatus.choices,
        default=ContentStatus.DRAFT, db_index=True,
    )
    ref_guide = models.ForeignKey(
        "guides.Guide", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="posts", verbose_name=_("Linked guide"),
    )
    # NOTE: listings app does not exist yet (Prompt 17). The FIELD is created
    # now so references stay typed (a listing pk, not a URL); the preview is
    # an honest NO_DATA placeholder until Prompt 17 lands.
    ref_listing = models.PositiveBigIntegerField(
        _("Linked listing"), null=True, blank=True,
        validators=[MinValueValidator(1)],
    )
    images = models.ManyToManyField(
        "media.MediaRef", through="PostImage", related_name="posts", blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.author_id}: {self.text[:40]}"

    @property
    def chars_remaining(self) -> int:
        """How many characters the author still has (form hint + tests)."""
        return max(0, MAX_POST_CHARS - len(self.text))

    def clean(self):
        # The image cap lives on the MODEL (PROMPTS.md pitfall), not just the
        # upload endpoint: any future write path (admin, API, script) that
        # ends up with a 5-photo post fails validation here. For a not-yet-
        # saved post the through rows cannot exist, so the check is pk-gated;
        # per-attachment inserts are gated in PostImage.clean().
        if self.pk and self.attachments.count() > MAX_IMAGES:
            raise ValidationError(
                {"images": MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}}
            )


class PostImage(TimestampedModel):
    """One ordered photo slot of a post (position 1..4, no gaps enforced —
    gaps may exist after display-only cases; only the CAP and uniqueness are
    hard)."""

    post = models.ForeignKey(
        Post, on_delete=models.CASCADE, related_name="attachments"
    )
    image = models.ForeignKey(
        "media.MediaRef", on_delete=models.CASCADE, related_name="post_images"
    )
    position = models.PositiveSmallIntegerField(_("Position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["post", "position"], name="uniq_post_position"
            ),
            # One photo once per post — no accidental double-attach.
            models.UniqueConstraint(
                fields=["post", "image"], name="uniq_post_image"
            ),
        ]

    def __str__(self) -> str:
        return f"post {self.post_id} #{self.position}"

    def clean(self):
        if not (1 <= self.position <= MAX_IMAGES):
            raise ValidationError(
                {"position": MSG_BAD_POSITION % {"n": MAX_IMAGES}}
            )
        if not self.post_id:
            return  # foreign-key integrity errors will catch this first
        siblings = PostImage.objects.filter(post_id=self.post_id)
        if self.pk:
            siblings = siblings.exclude(pk=self.pk)
        if siblings.filter(position=self.position).exists():
            raise ValidationError({"position": MSG_POSITION_TAKEN})
        if self.image_id and siblings.filter(image_id=self.image_id).exists():
            raise ValidationError({"image": MSG_DUPLICATE_PHOTO})
        if not self.pk and siblings.count() >= MAX_IMAGES:
            raise ValidationError(
                {"image": MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES}}
            )
