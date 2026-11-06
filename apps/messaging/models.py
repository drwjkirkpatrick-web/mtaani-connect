"""Messaging v1 models (Prompt 20): threads, membership, messages.

Shape (documented for the parent-consolidation reader):
- Thread             1-to-1 conversation; carries a TYPED origin reference
                     (origin_kind + origin_listing_id). Title is auto-derived
                     from the origin at creation.
- ThreadParticipant  membership through-table; the receipts / unread / mute
                     state of ONE member in ONE thread lives HERE (never on
                     the user, never on the thread).
- Message            immutable plain-text message (≤2000 chars, ≤4 photos).
- MessageImage       one ordered photo slot per message (through media.MediaRef).

SAFETY CONTRACT (PROMPTS.md pitfall): every message/thread read or write must
start from Thread.objects.for_participant(user) — guessing a thread ID is the
classic IDOR hole. Views enforce it; the model belt-and-braces: Message.clean()
refuses senders who are not members, so even future admin/script write paths
inherit the gate.

ORIGIN CONTRACT — deliberately NO FK to listings.Listing: that app is being
built CONCURRENTLY in this wave and its table may not exist when these
migrations run in sibling test DBs. origin_listing_id is a plain
PositiveBigIntegerField (Prompt 21's barter origin will add a new
origin_kind value later — plain .value strings need no migration). The UI
renders the honest "Listing #<pk>" label until the parent's consolidation
step upgrades the reference (and links it) to the real listing.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel

from .phone import phone_shapes

MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 2000
MAX_IMAGES = 4

MSG_MESSAGES_IMMUTABLE = _("Messages are immutable — an honest chat leaves no edits.")
MSG_NOT_A_PARTICIPANT = _("Only thread members can act on this conversation.")
MSG_TOO_MANY_IMAGES = _("A message can carry at most %(n)d photos.")
MSG_BAD_POSITION = _("Photo positions run from 1 to %(n)d.")
MSG_EMPTY_MESSAGE = _("Write a message or attach a photo.")


class ThreadOrigin(models.TextChoices):
    """Why a thread exists (typed origin reference, verbatim Prompt 20)."""

    LISTING = "listing", _("Marketplace listing enquiry")
    DIRECT = "direct", _("Direct conversation")
    # NOTE: 'barter' (negotiation origin) arrives with Prompt 21 — values are
    # stored as plain .value strings, so adding it later needs no migration.


class ThreadQuerySet(models.QuerySet):
    """Shared reads start here — the membership gate is ONE helper, not a
    convention each route reinvents."""

    def for_participant(self, user):
        """Threads where `user` is a member. Anonymous / None → empty.

        WHY the IDOR shape matters: a non-member must see the same answer as
        a genuinely absent thread (NOT a 403 "you lack access"), because a
        distinguishable error confirms existence to a thread-id guesser.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(participants=user)


class Thread(TimestampedModel):
    """One 1-to-1 conversation. Members live in ThreadParticipant (through)."""

    title = models.CharField(_("Title"), max_length=MAX_TITLE_CHARS)
    origin_kind = models.CharField(
        _("Origin kind"),
        max_length=16,
        choices=ThreadOrigin.choices,
        default=ThreadOrigin.DIRECT,
        db_index=True,
    )
    # TYPED origin reference (no FK — see module docstring). db_index because
    # the enquiry flow looks up "has this buyer already enquired about this
    # listing?" on every start POST.
    origin_listing_id = models.PositiveBigIntegerField(
        _("Origin listing id (no FK while listings builds concurrently)"),
        null=True,
        blank=True,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="threads_started",
        verbose_name=_("Started by"),
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="ThreadParticipant",
        related_name="threads",
        verbose_name=_("Members"),
    )

    objects = ThreadQuerySet.as_manager()  # the membership gate lives HERE

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.CheckConstraint(
                # WHY a DB-level mirror: origin integrity must not depend on
                # which app wrote the row (parent consolidation FKs it later).
                condition=models.Q(origin_kind="listing", origin_listing_id__isnull=False)
                | models.Q(origin_kind="direct", origin_listing_id__isnull=True),
                name="thread_origin_matches_kind",
            )
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.origin_kind}: {self.title[:40]}"

    def clean(self):
        # DB constraint mirrors this too; the model check exists so full_clean
        # surfaces the reason to callers (services/forms), not a bare error.
        listing_origin = self.origin_kind == ThreadOrigin.LISTING.value
        if listing_origin and self.origin_listing_id is None:
            raise ValidationError(
                {"origin_listing_id": _("A listing enquiry needs its listing reference.")}
            )
        if not listing_origin and self.origin_listing_id is not None:
            raise ValidationError(
                {"origin_listing_id": _("Only listing-based threads carry a listing reference.")}
            )


class ThreadParticipant(TimestampedModel):
    """One member's per-thread state (Prompt 20 contract: receipts, unread,
    mute all live per participation — KDPA-minimal: nothing user-global)."""

    thread = models.ForeignKey(
        # WHY 'memberships' (not '+'): the list view must constrain mute state
        # through the VIEWER'S OWN through-row (filter(memberships__user=...,
        # memberships__muted=...)); '+' would also kill that query path, and
        # reusing the default name would clash with the M2M (fields.E302).
        "messaging.Thread",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="thread_memberships",
    )
    # NOTE: contract-named column (Prompt 20) — mirrors created_at; kept as its
    # own column so the receipts/unread contract is explicit at query sites.
    joined_at = models.DateTimeField(_("Joined at"), auto_now_add=True, db_index=True)
    # Watermark for BOTH unread counts (strictly-newer messages count) and
    # read receipts (>= counts as seen). Set when the member views the thread.
    last_read_at = models.DateTimeField(_("Last read at"), null=True, blank=True)
    muted = models.BooleanField(_("Muted"), default=False, db_index=True)
    # Read receipts opt-out, per member per thread (default ON — visible when
    # BOTH members opted in; the hiding rule is honest, not inferred).
    show_receipts = models.BooleanField(_("Show read receipts"), default=True)

    class Meta:
        ordering = ["thread", "user"]
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "user"], name="uniq_thread_member"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} in #{self.thread_id}"


class Message(TimestampedModel):
    """An immutable message. Body is PLAIN TEXT: templates render with
    |linebreaks and NEVER |safe — user HTML cannot exist anywhere in chat."""

    thread = models.ForeignKey(
        "messaging.Thread", on_delete=models.CASCADE, related_name="messages"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="messages_sent",
        verbose_name=_("Sent by"),
    )
    body = models.TextField(
        _("Message"),
        blank=True,
        default="",
        max_length=MAX_BODY_CHARS,
        validators=[MaxLengthValidator(MAX_BODY_CHARS)],
    )
    images = models.ManyToManyField(
        "media.MediaRef",
        through="MessageImage",
        related_name="messages",
        verbose_name=_("Photos"),
    )
    # Save-time metadata for moderation analytics ONLY (Prompt 17 phone
    # patterns): set in save(); the message still sends. See phone.py.
    is_phone_flagged = models.BooleanField(
        _("Phone-shaped text"), default=False, db_index=True, editable=False
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"msg {self.pk} in #{self.thread_id} by {self.sender_id}"

    @property
    def chars_remaining(self) -> int:
        """Live typing hint on the model (Prompt 15/20 contract parity)."""
        return max(0, MAX_BODY_CHARS - len(self.body))

    def clean(self):
        # BELT-AND-BRACES membership gate (module docstring): the sender must
        # be a member of the thread on EVERY write path, including scripts.
        if self.thread_id and self.sender_id:
            member = ThreadParticipant.objects.filter(
                thread_id=self.thread_id, user_id=self.sender_id
            ).exists()
            if not member:
                raise ValidationError({"sender": MSG_NOT_A_PARTICIPANT})

    def save(self, *args, **kwargs):
        # IMMUTABLE messages (Prompt 20: "NO edits — honest audit"): creation
        # allowed, re-save refused — same precedent as moderation audit rows.
        if not self._state.adding:
            raise ValidationError(MSG_MESSAGES_IMMUTABLE)
        # Save-time phone-shape flag (metadata only — see phone.py): compute
        # BEFORE the INSERT so the very first persisted row already carries it.
        self.is_phone_flagged = phone_shapes(self.body)
        super().save(*args, **kwargs)


class MessageImage(TimestampedModel):
    """One ordered photo slot of a message (position 1..≤4). The cap is
    enforced here AND in services.send_message — model-side so no future write
    path can attach a 5th photo, service-side so the user gets the reason."""

    message = models.ForeignKey(
        "messaging.Message", on_delete=models.CASCADE, related_name="image_refs"
    )
    image = models.ForeignKey(
        "media.MediaRef",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="message_images",
        verbose_name=_("Photo"),
    )
    position = models.PositiveSmallIntegerField(_("Position"))

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["message", "position"], name="uniq_message_image_position"
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1) & models.Q(position__lte=MAX_IMAGES),
                name="message_image_position_1_to_4",
            ),
        ]

    def __str__(self) -> str:
        return f"msg {self.message_id} photo #{self.position}"

    def clean(self):
        if not (1 <= self.position <= MAX_IMAGES):
            raise ValidationError({"position": MSG_BAD_POSITION % {"n": MAX_IMAGES}})
        if self.message_id and not self.pk:
            siblings = MessageImage.objects.filter(
                message_id=self.message_id
            ).exclude(pk=self.pk)
            if siblings.count() >= MAX_IMAGES:
                raise ValidationError(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES})
