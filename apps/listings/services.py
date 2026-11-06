"""Listing services (Prompt 17) — one door for writes, public read API.

Consumers read through THIS module, never the model internals directly:
- visible_listings(): the public market surface (PUBLISHED, REMOVED excluded,
  sellers the viewer has blocked excluded — via moderation's PUBLIC helper).
- get_visible_listing(pk, for_user): the single-row read policy (DRAFT /
  UNDER_REVIEW / REMOVED stay seller+staff-private).
- detect_phone_patterns(text) -> list[str]: the Prompt 17 pitfall detector —
  Kenyan mobile shapes in public text. Prompt 20 (messaging) consolidates its
  per-message detector on top of this function; it is deliberately a
  standalone module-level function (pure, no DB) so that reuse stays cheap.

Publish decision: listings publish ON CREATE (same one-tap philosophy as
posts — "photograph, price, done"). DRAFT stays available for future
workflows; moderation layers UNDER_REVIEW via the review_flagged queue.

Prompt 24 adds the TRADING axis (see apps/listings/lifecycle.py for the
pure rules + transition table): pause/resume/mark_sold/renew/bump are
seller one-taps with NO moderator in the loop, the 30-day expiry runs as
an injectable-clock sweep (cron hook), and a category edit on a live row
flips the row into moderation re-approval. Existing Prompt 17 functions
keep their signatures and behavior untouched.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import DateTimeField
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, ReportReason
from apps.media.models import MediaRef
from apps.moderation.models import MAX_NOTE_CHARS
from apps.moderation.services import RateLimitError, report_content

from . import lifecycle as lc  # lifecycle.py: pure table + assertions (Prompt 24)
from .lifecycle import EXPIRY_DAYS, LifecycleError

from .models import (
    CATEGORY_VALUES,
    MAX_IMAGES,
    MSG_TOO_MANY_IMAGES,
    Condition,
    Listing,
    ListingImage,
    ListingLifecycle,
)

PUBLISHED = ContentStatus.PUBLISHED.value

# ---------------------------------------------------------------------------
# Phone-pattern detector (Prompt 17 pitfall — the #1 scam vector).
#
# WHY two shapes: Kenyan mobiles are written as 07XX/01XX XXX XXX (national
# form — 01XX is the newer mobile range, same intent) or +254 7XX/1XX XXX XXX
# (international form), with spaces/dashes as separators.
# The alternation matches BOTH; the anchors keep phone-shaped words inside
# longer tokens (price codes, dates like "07:00", "2019-07-15", "VIN0712…")
# from matching. NOT auto-blocking: a match queues human review only — people
# legitimately discuss phone-shaped things in public text.
# NOTE (Prompt 20 consolidation): the 01XX range came over from messaging's
# save-time flagger so ONE detector serves listings AND message analytics.
_PHONE_SHAPES = re.compile(
    r"(?<!\w)\+?254[-\s]?[71]\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"   # +254 7XX/1XX XXX XXX
    r"|(?<!\w)0[-\s]?[71]\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"       # 07XX/01XX XXX XXX
)


def detect_phone_patterns(text: str) -> list[str]:
    """Return the distinct Kenyan-mobile-shaped substrings in `text`, in
    order of first appearance ([] when none).

    Pure function — no DB, no user context — so moderation analytics (Prompt
    20) can reuse it on message bodies too.
    """
    if not text:
        return []
    seen: set[str] = set()
    found: list[str] = []
    for match in _PHONE_SHAPES.finditer(text):
        if match.group() not in seen:
            seen.add(match.group())
            found.append(match.group())
    return found


AUTO_FLAG_NOTE_PREFIX = _("Auto-flagged by the phone-pattern detector:")


def _auto_flag_note(matches: list[str]) -> str:
    # WHY truncation: Report.note is bounded (500 chars) and the report must
    # never fail for a runaway description full of phone-shaped strings.
    return (
        f"{AUTO_FLAG_NOTE_PREFIX} {', '.join(matches)}"[:MAX_NOTE_CHARS]
    )


def flag_listing_phone_patterns(listing: Listing) -> list[str]:
    """Scan the listing's PUBLIC text fields for phone shapes; queue the
    result for human moderators. Returns the matched strings.

    Two-step design (PROMPTS.md pitfall): 1) set Listing.review_flagged —
    cheap, always works, visible to the seller; 2) hand a Report to the
    Prompt 15 queue via its PUBLIC report_content service. The report is
    best-effort: it is rate-limited (10/h/reporter, Prompt 15) and its target
    allow-list is extended app-by-app — a rate-limited or not-yet-allowed
    report must NEVER crash the listing save. WHY the listing stays live:
    Prompt 17 explicitly forbids auto-blocking; humans decide.
    """
    public_text = "\n".join(
        field
        for field in (
            listing.title,
            listing.title_sw,
            listing.description,
            listing.description_sw,
        )
        if field
    )
    matches = detect_phone_patterns(public_text)
    if not matches:
        if listing.review_flagged:
            listing.review_flagged = False
            listing.save(update_fields=["review_flagged", "updated_at"])
        return []
    listing.review_flagged = True
    listing.save(update_fields=["review_flagged", "updated_at"])
    try:
        # NOTE: the reporter is the listing's own seller because Report
        # REQUIRES an authenticated member and this row is system-initiated;
        # the note tells moderators it is an auto-flag. (A dedicated
        # system/reporting actor would need a parent-owned change.)
        report_content(
            reporter=listing.seller,
            target=listing,
            reason=ReportReason.SCAM.value,
            note=_auto_flag_note(matches),
        )
    except RateLimitError:
        # NOTE: 10 reports/hour exhausted — skip, the seller-visible flag
        # already shows, and the queue sees the next report in the window.
        pass
    except ValidationError:
        # NOTE: any other service-level refusal (e.g. 'listings' not yet in
        # the moderation allow-list — see apps/moderation/models.py) must not
        # block the seller's save. The flag remains; the queue still sees
        # future reports once the allow-list is extended.
        pass
    return matches


# --- public read API (views + future search Prompt 19, feed consumers) ------


def visible_listings(*, viewer=None, category: str | None = None, q: str | None = None):
    """The market list surface: PUBLISHED rows only, blocked sellers hidden
    for the viewer, newest first. `category` filters on a LISTING_CATEGORIES
    .value; `q` searches titles (en + sw). REMOVED is never public.

    Prompt 24, additive, no signature change:
    - AND lifecycle ACTIVE — the trading axis. Prompt 17's rows default to
      ACTIVE, so its list tests read unchanged; PAUSED/SOLD/EXPIRED exit the
      public list while staying reachable on the (own) detail page.
    - AND bump-aware ORDER — position key is coalesce(bumped_at, created_at)
      descending (the anchor the bump cap shares — one rule everywhere), so
      a capped bump is the ONLY way to the top. created_at breaks ties so
      equal keys render deterministically.
    """
    listings = (
        Listing.objects.filter(
            status=PUBLISHED, lifecycle=ListingLifecycle.ACTIVE.value
        )
        # WHY an annotated key (not .order_by raw fields): a bumped_at NULL
        # must fall back to created_at IN SQL — Python-side sorting would
        # paginate wrong. Coalesce computes the key per row.
        .annotate(
            order_key=Coalesce("bumped_at", "created_at",
                               output_field=DateTimeField())
        )
        .order_by("-order_key", "-created_at")
        .select_related("seller")
    )
    listings = _moderation_visible(viewer, listings)
    if category:
        listings = listings.filter(category=category)
    if q:
        listings = listings.filter(title__icontains=q)
    return listings


def _moderation_visible(viewer, queryset):
    """Delegate the viewer-scoped visibility rule to moderation's PUBLIC
    helper, never re-implement block/removed policy app-locally."""
    from apps.moderation.services import visible  # noqa: PLC0415 — avoids cycle risk

    return visible(viewer, queryset, author_field="seller")


def get_visible_listing(listing_id: int, for_user) -> Listing | None:
    """Single-listing read policy: PUBLISHED is public; DRAFT / UNDER_REVIEW
    / REMOVED stay private to the seller (staff too — moderation preview).
    None = 404 in the view — keep the policy HERE so every route answers
    the same way."""
    try:
        listing = Listing.objects.select_related("seller").get(pk=listing_id)
    except Listing.DoesNotExist:
        return None
    is_public = listing.status == PUBLISHED
    is_own = getattr(for_user, "pk", None) == listing.seller_id
    is_staff = getattr(for_user, "is_staff", False)
    if not is_public and not (is_own or is_staff):
        return None
    return listing


# --- writes ------------------------------------------------------------------


def _assert_seller(user, listing: Listing, action: str) -> None:
    """Seller-only gate for listing changes (staff may moderate)."""
    if user != listing.seller and not getattr(user, "is_staff", False):
        raise PermissionDenied(f"Only the seller can {action} this listing.")


def _valid_category_or_none(category) -> str | None:
    """Guard against an unknown category reaching storage from any path."""
    if category in CATEGORY_VALUES:
        return category
    return None


@transaction.atomic
def create_listing(
    *,
    seller,
    title: str,
    description: str,
    category: str,
    ward: str,
    condition: str | None = None,
    price: Decimal | None = None,
    price_negotiable: bool = False,
    title_sw: str = "",
    description_sw: str = "",
    status: str = PUBLISHED,
) -> Listing:
    """Create + (normally) publish in one tap, then run the pitfall detector.
    full_clean keeps every model rule enforced no matter which surface
    calls in; the status param stays private to tests/defaults — publish is
    the member flow."""
    listing = Listing(
        seller=seller,
        title=title,
        title_sw=title_sw or "",
        description=description,
        description_sw=description_sw or "",
        category=_valid_category_or_none(category),
        condition=condition or Condition.GOOD,
        price=price,
        price_negotiable=price_negotiable,
        ward=ward,
        status=status,
    )
    listing.full_clean()
    listing.save()
    flag_listing_phone_patterns(listing)
    listing.refresh_from_db(fields=["review_flagged"])
    return listing


@transaction.atomic
def edit_listing(*, listing: Listing, editor, **fields) -> Listing:
    """Seller-only edit. Status is deliberately NOT editable here — unpublish
    is the moderation/soft-remove path (remove_listing → REMOVED). The
    detector re-runs: edited text can add OR clear a phone-pattern match
    (the flag always reflects the CURRENT public text).

    Prompt 24 consolidation: ANY seller category change on a live market
    row now goes through the SAME gated door the /category/ route uses
    (bait-and-switch prevention) — the main edit form is no longer a
    bypass. Invalid/unchanged categories keep the old silent no-op.
    """
    _assert_seller(editor, listing, "edit")
    # WHY refresh first: ModelForm callers pre-mutate the bound instance
    # (Django's _post_clean assigns cleaned fields to it), which would hide
    # the category gate from stored-value comparison. Start from the STORED
    # row; every provided field is re-applied below (the view passes the
    # full cleaned_data, so nothing is lost).
    listing.refresh_from_db()
    category = fields.pop("category", None)
    if (
        category is not None
        and _valid_category_or_none(category) is not None
        and category != listing.category
    ):
        listing = edit_listing_category(
            listing=listing, editor=editor, category=category
        )
    for name in ("title", "title_sw", "description", "description_sw",
                 "price", "price_negotiable", "ward", "condition"):
        if name in fields:
            setattr(listing, name, fields[name])
    listing.full_clean()
    listing.save()
    flag_listing_phone_patterns(listing)
    listing.refresh_from_db(fields=["review_flagged"])
    return listing


def remove_listing(listing: Listing, *, by_user) -> Listing:
    """Soft delete: status REMOVED hides the listing everywhere while rows
    survive for moderation/dispute evidence (never hard delete)."""
    _assert_seller(by_user, listing, "remove")
    listing.status = ContentStatus.REMOVED.value
    listing.save(update_fields=["status", "updated_at"])
    return listing


def listing_image_count(listing: Listing) -> int:
    """The number the upload endpoint re-checks server-side before accepting
    another image (client-side checks are decoration — PROMPTS.md pitfall)."""
    return listing.attachments.count()


def add_listing_image(listing: Listing, image: MediaRef) -> ListingImage:
    """Attach ONE already-processed MediaRef (Prompt 06 pipeline output) to
    the first free slot. Never accepts raw files — uploads go through the
    endpoint, which calls this AFTER its own cap check (defense in depth)."""
    if image.owner_id != listing.seller_id:
        # KDPA: you attach your own media, never someone else's upload.
        raise PermissionDenied("That image belongs to another member.")
    if listing_image_count(listing) >= MAX_IMAGES:
        raise ValidationError(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES})
    taken = set(listing.attachments.values_list("position", flat=True))
    positions = set(range(1, MAX_IMAGES + 1)) - taken
    row = ListingImage(listing=listing, image=image, position=min(positions))
    row.full_clean()  # slot legality + THE per-attachment cap on the model
    row.save()
    return row


# ===========================================================================
# Lifecycle services (Prompt 24). Every lifecycle change goes through HERE
# — validated against lifecycle.py's pure TRANSITIONS table, never by
# hand-rename — and gated to the seller via the Prompt 17 gate (staff keep
# their moderation allowance). NO moderator in any of these loops: the
# acceptance is that listings age, pause, sell and renew without moderator
# load. The single human gate is the category re-approval further down.

# Stamp fields every transition persists (update_fields keeps the row's
# other columns from being rewritten by concurrent writes... WHY explicit:
# transitions are one-tap deltas, not full-form saves).
_TRANSITION_FIELDS = ("lifecycle", "updated_at")

# SOURCE-state pins: the EDGES in lifecycle.py say a move is possible; THESE
# say which one-tap may make it. WHY needed at all: EXPIRED→ACTIVE is a
# legal edge (the renew), but a plain resume() must NOT be able to take it
# for free — "back on the market" means the seller re-stamped the row.
# Same shape guards every tool; only mark_sold has two legal sources.


def _require_current(listing: Listing, from_value: str) -> None:
    if listing.lifecycle != from_value:
        raise LifecycleError(lc.MSG_WRONG_SOURCE % {
            "from_state": listing.lifecycle, "from_value": from_value,
        })


@transaction.atomic
def pause_listing(*, listing: Listing, by_user, now=None) -> Listing:
    """ACTIVE → PAUSED: off the market, content kept byte-for-byte. The
    seller resumes whenever — no moderator, no re-typing. paused_at marks
    the pause (resume clears it; it is a current-state stamp, not a log).

    NOTE: pause does NOT touch renewed_at. The expiry anchor stays the
    row's original (renewed_at else created_at), so a very long pause that
    resumes with a stale anchor simply expires on the next sweep — the
    same one-tap RENEW is the fresh start. Minimal, and the 30-day
    contract stays with the sweep alone."""
    _assert_seller(by_user, listing, "pause")
    _require_current(listing, ListingLifecycle.ACTIVE.value)
    lc.assert_transition(listing.lifecycle, ListingLifecycle.PAUSED.value)
    listing.lifecycle = ListingLifecycle.PAUSED.value
    listing.paused_at = now or timezone.now()
    listing.save(update_fields=[*_TRANSITION_FIELDS, "paused_at"])
    return listing


@transaction.atomic
def resume_listing(*, listing: Listing, by_user, now=None) -> Listing:
    """PAUSED → ACTIVE (resume) — and ONLY from PAUSED: EXPIRED comes back
    strictly via the one-tap renew (fresh timestamp), so a resume can never
    smuggle a stale row past its 30-day window for free."""
    _assert_seller(by_user, listing, "resume")
    _require_current(listing, ListingLifecycle.PAUSED.value)
    lc.assert_transition(listing.lifecycle, ListingLifecycle.ACTIVE.value)
    listing.lifecycle = ListingLifecycle.ACTIVE.value
    listing.paused_at = None
    listing.save(update_fields=[*_TRANSITION_FIELDS, "paused_at"])
    return listing


@transaction.atomic
def mark_listing_sold(*, listing: Listing, by_user, now=None) -> Listing:
    """ACTIVE or PAUSED → SOLD — terminal from the seller. WHY terminal:
    the row is Prompt 22 reviews' + Prompt 23 disputes' audit trail; a
    re-list of the same item is a NEW listing (create_listing), never a
    resurrection of this row. sold_at is the deal-completion fact."""
    _assert_seller(by_user, listing, "mark sold")
    lc.assert_transition(listing.lifecycle, ListingLifecycle.SOLD.value)
    listing.lifecycle = ListingLifecycle.SOLD.value
    listing.sold_at = now or timezone.now()
    listing.save(update_fields=[*_TRANSITION_FIELDS, "sold_at"])
    return listing


@transaction.atomic
def renew_listing(*, listing: Listing, by_user, now=None) -> Listing:
    """THE one-tap renew: EXPIRED → ACTIVE with IDENTICAL content and a
    fresh renewed_at (the sweep's new age anchor). No re-typing, no
    moderator, no new row — "still trading" is one fact, not a re-list.
    bumped_at is deliberately untouched: bump is the POSITION clock with
    its own cap, renewal is the MARKET-AGE clock."""
    _assert_seller(by_user, listing, "renew")
    _require_current(listing, ListingLifecycle.EXPIRED.value)
    lc.assert_transition(listing.lifecycle, ListingLifecycle.ACTIVE.value)
    listing.lifecycle = ListingLifecycle.ACTIVE.value
    listing.renewed_at = now or timezone.now()
    listing.save(update_fields=[*_TRANSITION_FIELDS, "renewed_at"])
    return listing


@transaction.atomic
def bump_listing(*, listing: Listing, by_user, now=None) -> Listing:
    """Refresh the row's list position: bumped_at = now, capped ONCE per
    BUMP_COOLDOWN_DAYS.

    WHY the anchor is coalesce(bumped_at, created_at) — the SAME key the
    market list orders by: the cap throttles exactly what a bump moves
    (position). A row never bumped anchors on its creation, so a
    just-created listing is ALREADY at the top of the feed and an
    immediate first bump is refused — utility-first, no shout-echo.
    Boundary: at EXACTLY the cooldown elapsed the bump goes through (the
    mirror image of the expiry sweep's at-exactly-30d move).

    No pay path exists or belongs here (no paid tier on this model — free,
    throttled self-service is the whole economy)."""
    _assert_seller(by_user, listing, "bump")
    _require_current(listing, ListingLifecycle.ACTIVE.value)
    now = now or timezone.now()
    anchor = listing.bumped_at or listing.created_at
    if anchor is not None and anchor > now - timedelta(days=lc.BUMP_COOLDOWN_DAYS):
        raise LifecycleError(
            lc.MSG_BUMP_TOO_SOON % {"cooldown": lc.BUMP_COOLDOWN_DAYS}
        )
    listing.bumped_at = now
    listing.save(update_fields=["bumped_at", "updated_at"])
    return listing


# --------------------------------------------------------------------------
# Category-edit gate (Prompt 24 edit rules).
#
# edit_listing (above) keeps Prompt 17's contract — price/description and
# the rest of the form edit freely while the row is live, and P17's pinned
# round-trip test (route POST, category changed, row stays PUBLISHED) is
# the regression the parent requires. So the GATE is its own door:
# edit_listing_category, exposed on its own seller route. Bait-and-switch
# prevention rides the EXISTING moderation columns — status UNDER_REVIEW +
# review_flagged (NO moderation/disputes internals touched; the queue
# integration is documented in the final report, not built here).


@transaction.atomic
def edit_listing_category(*, listing: Listing, editor, category: str) -> Listing:
    """Seller changes the category through the gated door.

    - Same value again → no-op (no event, no message noise).
    - Change on a LIVE market row (PUBLISHED + ACTIVE, the pure predicate
      in lifecycle.py) → category saved, row flipped ContentStatus.
      UNDER_REVIEW + review_flagged=True: PUBLIC until re-approved, seller
      keeps the row (get_visible_listing already lets seller+staff see
      UNDER_REVIEW — no new read machinery). WHY the category is still
      saved: the seller's intent is recorded; the row comes back exactly
      as approved — no silent discard, no dead-end re-typing.
    - Changes anywhere else (draft, already UNDER_REVIEW, REMOVED, EXPIRED
      — rows off the public market are not buyer-facing deception) →
      category saved plainly.
    """
    _assert_seller(editor, listing, "change the category of")
    value = _valid_category_or_none(category)
    if value is None:
        raise ValidationError(lc.MSG_NOT_A_CATEGORY)
    if value == listing.category:
        return listing
    gated = lc.category_edit_requires_reapproval(
        status=listing.status,
        lifecycle=listing.lifecycle,
        current_category=listing.category,
        new_category=value,
    )
    listing.category = value
    if gated:
        # NOTE: lifecycle stays ACTIVE (the SELLER still wants to trade);
        # the MODERATION axis flips — two axes, independent writers. The
        # pending state lives on category_gate (NOT review_flagged — that
        # flag means "phone detector matched current text" and the detector
        # legitimately clears it on edits). status UNDER_REVIEW is what
        # actually hides the row (services.get_visible_listing). Moderators
        # clear both via approve_recategorize (moderation queue/Prompt 37).
        listing.status = ContentStatus.UNDER_REVIEW.value
        listing.category_gate = True
        listing.save(
            update_fields=["category", "status", "category_gate", "updated_at"]
        )
        return listing
    listing.save(update_fields=["category", "updated_at"])
    return listing


# --- the 30-day sweep ------------------------------------------------------


def expire_stale_listings(*, now=None) -> int:
    """Age-expire the market (cron/admin hook; service + its tests). Moves
    PUBLISHED + lifecycle-ACTIVE rows whose age anchor — renewed_at when
    set (the last one-tap renew), else created_at — is at or beyond
    EXPIRY_DAYS. Returns the NUMBER of rows moved.

    Boundary contract (mirroring barter's sweep): at EXACTLY EXPIRY_DAYS
    elapsed the row moves (anchor <= now - window); tests pin it with an
    injected clock. PAUSED rows are never swept — pause is the seller's
    own hold, and resuming with a stale anchor is the seller's one-tap
    renew problem. No moderator anywhere in this loop."""
    now = now or timezone.now()
    window = timedelta(days=EXPIRY_DAYS)
    # WHY annotate + pks + bulk update instead of row-by-row saves: the
    # sweep is a cron-scale job — one SQL scan with a Coalesce anchor, one
    # bulk move; per-row save() would also rewrite every stamp column.
    stale = (
        Listing.objects.filter(
            status=PUBLISHED, lifecycle=ListingLifecycle.ACTIVE.value
        )
        .annotate(
            age_anchor=Coalesce("renewed_at", "created_at",
                                output_field=DateTimeField())
        )
        .filter(age_anchor__lte=now - window)
    )
    stale_pks = list(stale.values_list("pk", flat=True))
    if not stale_pks:
        return 0
    return Listing.objects.filter(pk__in=stale_pks).update(
        lifecycle=ListingLifecycle.EXPIRED.value, updated_at=now
    )


# --- moderator re-approval (Prompt 24 gate's other half) --------------------



@transaction.atomic
def approve_recategorize(listing: Listing, *, by_user, now=None) -> Listing:
    """A moderator CONFIRMS the gated category change: the row returns to
    PUBLISHED in its NEW category, category_gate cleared, an immutable
    moderation audit row written. SELLERS cannot self-approve (only
    staff/moderators pass the gate — moderation's PUBLIC is_moderator).
    """
    from apps.moderation.models import AuditAction, ModeratorAudit
    from apps.moderation.services import is_moderator

    if not is_moderator(by_user):
        raise PermissionDenied("Only a moderator can approve a category change.")
    if not listing.category_gate:
        return listing  # nothing pending — idempotent
    listing.status = ContentStatus.PUBLISHED.value
    listing.category_gate = False
    listing.save(update_fields=["status", "category_gate", "updated_at"])
    ModeratorAudit.objects.create(
        actor=by_user,
        action=AuditAction.RECAT_APPROVED,
        target_ct=ContentType.objects.get_for_model(listing),
        target_id=listing.pk,
    )
    return listing
