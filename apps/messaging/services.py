"""Messaging services (Prompt 20) — one door for every write + shared reads.

Every helper here presumes the CALLER has resolved objects through the
membership gate (Thread.objects.for_participant / get_thread_for). The gate
is the product: thread IDs are the classic IDOR hole (PROMPTS.md pitfall),
so no helper ever widens a query past a member's own participation.

Cross-app contract (Hard Rules): moderation writes go through the Prompt 15
PUBLIC door (report_content); listings reads go through the sibling's PUBLIC
API only, IF it is importable at runtime — apps.listings is building in a
parallel wave, so absence is an expected, handled state (never fabricated).
"""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ReportReason
from apps.media.services import save_upload  # Prompt 06 pipeline (EXIF strip)
from apps.moderation import services as moderation  # PUBLIC Prompt-15 door
from apps.moderation.models import (  # public models: Prompt 15 imports these in its own tests
    ALLOWED_TARGET_APPS,
    AuditAction,
    ModeratorAudit,
    Report,
    ReportStatus,
)

from .models import (
    MAX_IMAGES,
    MAX_TITLE_CHARS,
    MSG_NOT_A_PARTICIPANT,
    MSG_TOO_MANY_IMAGES,
    Message,
    MessageImage,
    Thread,
    ThreadOrigin,
    ThreadParticipant,
)

NEWEST_MESSAGE_WINDOW = (
    50  # newest messages rendered per thread page (mobile budget) — NOTE:
)  #     full-history pagination arrives later; not a correctness limit.

MSG_EMPTY_MESSAGE = _("Write a message or attach a photo.")
MSG_BAD_REASON = _("Pick a reason for the report from the list.")
MSG_OWN_LISTING = _("You cannot start an enquiry conversation on your own listing.")
MSG_SELF_THREAD = _("You cannot start a conversation with yourself.")


class ListingUnavailable(Exception):
    """A listing reference cannot be resolved against the sibling listings
    app YET (it builds concurrently) or the listing is genuinely gone.

    WHY a dedicated exception: callers render the honest "listing not
    available yet" page and NEVER fabricate a seller or a thread row.
    """

    def __init__(self, message=None):
        from django.utils.translation import gettext as _t  # runtime text

        self.message = (
            _t(message)
            if message
            else _t("This listing isn't available for messages yet.")
        )
        super().__init__(self.message)


# --------------------------------------------------------------------- reads


def get_thread_for(user, thread_id):
    """Membership-gated single-thread read. None == 'cannot see it at all' —
    the view turns that into a plain 404 (same answer as a missing thread,
    so ID guessing confirms nothing)."""
    try:
        return Thread.objects.for_participant(user).get(pk=thread_id)
    except Thread.DoesNotExist:
        return None


def participant_of(thread, user) -> ThreadParticipant | None:
    """The viewer's own membership row in this thread (state lives HERE)."""
    if thread is None or user is None or not getattr(user, "is_authenticated", False):
        return None
    return ThreadParticipant.objects.filter(
        thread_id=thread.pk, user_id=user.pk
    ).first()


def counterpart_of(thread, user) -> ThreadParticipant | None:
    """The OTHER member of a 1-to-1 thread (for display only — templates must
    never render their phone/email; contact via chat is the user's choice)."""
    if thread is None or user is None:
        return None
    return (
        ThreadParticipant.objects.filter(thread_id=thread.pk)
        .exclude(user_id=getattr(user, "pk", None))
        .select_related("user")
        .first()
    )


def unread_counts(threads, viewer) -> dict[int, int]:
    """thread_id -> how many inbound messages the viewer has NOT read.

    A message counts for a member when it is strictly newer than that
    member's last_read_at (their own sends never count). WHY not a queryset
    annotation on the list: the watermark lives on the viewer's OWN
    participant row and differs per thread — this is one aggregated query
    with OR-joined per-row conditions instead of N queries.
    NOTE (edge contract): strictly-newer means a message timestamped exactly
    AT the watermark is already read (the member saw it while viewing).
    """
    threads = list(threads)
    if not threads:
        return {}
    participations = ThreadParticipant.objects.filter(
        user=viewer, thread__in=threads
    )
    conditions = []
    for p in participations:
        window = Q(thread_id=p.thread_id)
        if p.last_read_at is not None:
            window &= Q(created_at__gt=p.last_read_at)
        conditions.append(window & ~Q(sender_id=viewer.pk))
    if not conditions:
        return {}
    combined = conditions[0]
    for cond in conditions[1:]:
        combined |= cond
    rows = (
        Message.objects.filter(combined)
        .values("thread_id")
        .annotate(unread=Count("id"))
    )
    return {row["thread_id"]: row["unread"] for row in rows}


def messages_window(thread, limit: int = NEWEST_MESSAGE_WINDOW):
    """The newest `limit` messages, rendered oldest-first. A thread page must
    stay inside the 50 KB mobile budget no matter how long a conversation is."""
    newest = list(thread.messages.values_list("pk", flat=True).order_by("-created_at")[:limit])
    return (
        thread.messages.filter(pk__in=newest)
        .order_by("created_at", "pk")
        .prefetch_related("image_refs__image")  # photo slots + variants, 2 queries
    )


def seen_flags(participation: ThreadParticipant, other, messages) -> dict[int, bool]:
    """message pk -> True when the OTHER member has read my message.

    HONEST RECEIPTS CONTRACT (opt-out per user): a "Seen" marker renders only
    when BOTH members still show receipts. The watermark itself is still
    tracked while hidden — hiding receipts is about display, not lying about
    it; unread counts remain correct.
    """
    if other is None or not (participation.show_receipts and other.show_receipts):
        return {}
    watermark = other.last_read_at
    if watermark is None:
        return {}
    return {
        m.pk: (m.sender_id == participation.user_id and watermark >= m.created_at)
        for m in messages
    }


def mark_read(participation: ThreadParticipant, now=None) -> None:
    """Set the member's read watermark to `now` (injectable clock)."""
    participation.last_read_at = timezone.now() if now is None else now
    participation.save(update_fields=["last_read_at", "updated_at"])


def set_receipts(participation: ThreadParticipant, show: bool) -> None:
    """Per-thread receipts opt in/out (the setting endpoint's whole body)."""
    participation.show_receipts = bool(show)
    participation.save(update_fields=["show_receipts", "updated_at"])


def set_muted(participation: ThreadParticipant, muted: bool) -> None:
    """Mute hides the thread from the member's OWN list (history stays via
    direct link — muting is personal, never content moderation)."""
    participation.muted = bool(muted)
    participation.save(update_fields=["muted", "updated_at"])


# -------------------------------------------------------------------- writes


@transaction.atomic
def create_enquiry_thread(*, initiator, listing_id, resolver=None):
    """Buyer -> seller marketplace-enquiry thread (the Prompt 20 origin).

    One thread per (buyer, listing): re-enquiries return the existing thread
    (an enquiry is not a spam-generating write). The seller comes from the
    listing's PUBLIC surface; when it can't be resolved we raise
    ListingUnavailable and the view renders the honest pending page.
    `resolver` is injectable (mock-first hard rule) for tests: a callable
    listing_id -> (owner_user, title) | None.
    """
    try:
        listing_id = int(listing_id)
    except (TypeError, ValueError):
        raise ListingUnavailable(_("That listing reference is not valid."))
    if listing_id <= 0:
        raise ListingUnavailable(_("That listing reference is not valid."))
    existing = Thread.objects.filter(
        origin_kind=ThreadOrigin.LISTING.value,
        origin_listing_id=listing_id,
        created_by=initiator,
    ).first()
    if existing is not None:
        return existing  # idempotency: one enquiry thread per buyer+listing
    resolved = (resolver or resolve_public_listing)(listing_id)
    if resolved is None:
        raise ListingUnavailable()
    owner, title = resolved
    if owner.pk == initiator.pk:
        raise ValidationError(MSG_OWN_LISTING)
    thread = Thread(
        created_by=initiator,
        origin_kind=ThreadOrigin.LISTING.value,
        origin_listing_id=listing_id,
        title=(title or f"Listing #{listing_id}")[:MAX_TITLE_CHARS],
    )
    thread.full_clean()
    thread.save()
    ThreadParticipant.objects.create(thread=thread, user=initiator)
    ThreadParticipant.objects.create(thread=thread, user=owner)
    return thread


@transaction.atomic
def create_direct_thread(*, initiator, counterpart, title=None):
    """A plain 1-to-1 thread (origin 'direct'). No V1 route creates these —
    Prompt 21's barter negotiation (and future direct-contact routes) call
    THIS service, so the origin contract is one place."""
    if counterpart.pk == initiator.pk:
        raise ValidationError(MSG_SELF_THREAD)
    existing = (
        # Pair guard: initiator is a member AND counterpart is a member of the
        # same DIRECT thread (two through-rows — two separate m2m filters are
        # exactly correct here).
        Thread.objects.filter(
            origin_kind=ThreadOrigin.DIRECT.value, participants=initiator
        )
        .filter(participants=counterpart)
        .first()
    )
    if existing is not None:
        return existing
    thread = Thread(
        created_by=initiator,
        origin_kind=ThreadOrigin.DIRECT.value,
        title=(title or counterpart.get_username() or "")[:MAX_TITLE_CHARS]
        or gettext_lazy("%(name)s & %(you)s")
        % {"name": counterpart.get_username(), "you": initiator.get_username()},
    )
    thread.full_clean()
    thread.save()
    ThreadParticipant.objects.create(thread=thread, user=initiator)
    ThreadParticipant.objects.create(thread=thread, user=counterpart)
    return thread


@transaction.atomic
def send_message(*, thread, sender, body, uploads=None):
    """Persist one message (+ its photo slots through the Prompt 06 pipeline).

    Membership gate lives here too: a sender who is not in the thread is a
    ValidationError, because EVERY write route resolves the thread via
    for_participant first — this is the same rule said twice, by design.
    NOTHING is persisted unless body AND all photos validate: one atomic
    unit, so a 5th photo cannot leave a half-written message behind.
    """
    body = (body or "").strip()
    uploads = [u for u in (uploads or []) if u]
    if not body and not uploads:
        raise ValidationError(MSG_EMPTY_MESSAGE)
    if participant_of(thread, sender) is None:
        raise ValidationError(MSG_NOT_A_PARTICIPANT)
    if len(uploads) > MAX_IMAGES:
        raise ValidationError(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES})
    message = Message(thread=thread, sender=sender, body=body)
    message.full_clean()  # membership + length gate at model level too
    message.save()  # sets the save-time phone-shape flag (phone.py)
    for position, upload in enumerate(uploads, start=1):
        ref = save_upload(file_obj=upload, owner=sender, purpose="message")
        slot = MessageImage(message=message, image=ref, position=position)
        slot.full_clean()
        slot.save()
    return message


def report_message(*, reporter, message, reason: str, note: str = "", now=None) -> Report:
    """Report a chat message into the Prompt 15 queue — the PUBLIC door only.

    Two states, both graceful for the reporter:
    - moderation.report_content (when 'messaging' is in their allow-list)
      records the report;
    - PHASE-RACE FALLBACK: moderation's ALLOWED_TARGET_APPS intentionally
      does not list 'messaging' yet ("extend this tuple when messages land" —
      their model docstring), and those internals are NOT mine to edit. Until
      the parent's consolidation, the SAME queue rows (OPEN report +
      REPORT_CREATED audit) are written directly via their public models,
      with the rate limit and constants imported from their module — never
      re-implemented. Parent deletes this branch when 'messaging' joins the
      allow-list.
    In BOTH cases a report by a non-member is refused (IDOR pitfall) and a
    successful report AUTO-MUTES the reporter's own participation.
    """
    if message.thread_id is None or message.pk is None:
        raise ValidationError(MSG_NOT_A_PARTICIPANT)
    try:
        reason_value = ReportReason(reason).value
    except ValueError as exc:
        raise ValidationError(MSG_BAD_REASON) from exc
    participation = participant_of(message.thread, reporter)
    if participation is None:
        # NOTE: the view answers 404 via the thread gate anyway; this service
        # check stands so NO write path can report a message of a thread the
        # reporter is not part of.
        raise ValidationError(MSG_NOT_A_PARTICIPANT)

    try:
        if message._meta.app_label in ALLOWED_TARGET_APPS:
            report = moderation.report_content(
                reporter=reporter, target=message, reason=reason_value, note=note, now=now
            )
        else:
            report = _fallback_report(reporter, message, reason_value, note, now)
    except ValidationError:
        raise  # rate limit (code='report_rate_limited') is handled by the view
    _auto_mute_reporter(participation)
    return report


def _fallback_report(reporter, message, reason_value, note, now) -> Report:
    """PHASE-RACE ONLY (delete at parent consolidation): mirror of the public
    door's queue write, byte-for-byte the same row shapes it would produce."""
    now = timezone.now() if now is None else now
    window_start = now - moderation.RATE_WINDOW
    recent = Report.objects.filter(
        reporter=reporter, created_at__gte=window_start
    ).count()
    if recent >= moderation.MAX_REPORTS_PER_HOUR:
        # Same limit, same constants, same code — the view's graceful catch
        # behaves identically no matter which path recorded the report.
        raise moderation.RateLimitError(
            _(
                "You have reached the hourly limit of %(limit)s reports. "
                "Please try again later."
            )
            % {"limit": moderation.MAX_REPORTS_PER_HOUR},
            code="report_rate_limited",
        )
    report = Report(
        reporter=reporter,
        reason=reason_value,
        note=(note or "").strip(),
        target_ct=ContentType.objects.get_for_model(message),
        target_id=message.pk,
        status=ReportStatus.OPEN.value,
    )
    report.save()
    ModeratorAudit.objects.create(
        actor=reporter,
        action=AuditAction.REPORT_CREATED.value,
        target_ct=report.target_ct,
        target_id=message.pk,
        reason=reason_value,
    )
    return report


def _auto_mute_reporter(participation: ThreadParticipant) -> None:
    """Prompt 20: reporting a message auto-mutes the thread FOR THE REPORTER
    (they keep access to their history; the muted section + unmute endpoint
    own the way back). It is self-mute, not a sanction."""
    set_muted(participation, True)


# ------------------------------------------------- sibling-listing resolution


# NOTE (parent consolidation): the listings app builds CONCURRENTLY; its
# public read function's final name is not pinned yet. First callable wins at
# runtime; the parent replaces this shim with the real API (or an FK) later.
LISTING_PUBLIC_GETTERS = (
    "get_public_listing",
    "public_listing",
    "get_listing",
    "listing_detail",
    "visible_listing",
)
_LISTING_OWNER_ATTRS = ("created_by", "owner", "seller", "user")
_LISTING_TITLE_ATTRS = ("display_title", "title_en", "title_sw", "title")


def resolve_public_listing(listing_id):
    """Read the sibling listing through its PUBLIC surface; returns
    (owner_user, title_or_None) or None = 'not resolvable YET'.

    WHY try/except around everything: an ImportError/AttributeError here is
    the expected transient state while apps.listings lands in this wave —
    the caller renders the honest pending page and never fabricates. A real
    'listing missing/removed' ALSO lands here (honest, per the no-fabrication
    rule); the parent's FK keeps history rendering 'Listing #<pk>' either way.
    """
    try:
        from apps.listings import services as listing_services  #PUBLIC door only
    except ImportError:
        return None
    getter = next((g for g in LISTING_PUBLIC_GETTERS if callable(getattr(listing_services, g, None))), None)
    if getter is None:
        return None
    try:
        listing = getattr(listing_services, getter)(listing_id)
    except Exception:  # noqa: BLE001 — the sibling owns its error shapes
        return None
    if not listing:
        return None
    owner = next((getattr(listing, a, None) for a in _LISTING_OWNER_ATTRS if getattr(listing, a, None) is not None), None)
    if owner is None or not getattr(owner, "pk", None):
        return None  # cannot establish the seller — no thread, no fabrication
    title = next((getattr(listing, a, None) for a in _LISTING_TITLE_ATTRS if getattr(listing, a, None)), None)
    if isinstance(title, dict):  # defensive for translation-string shapes
        title = None
    return (owner, str(title) if title else None)
