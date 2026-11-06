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
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, ReportReason
from apps.media.models import MediaRef
from apps.moderation.models import MAX_NOTE_CHARS
from apps.moderation.services import RateLimitError, report_content

from .models import (
    CATEGORY_VALUES,
    MAX_IMAGES,
    MSG_TOO_MANY_IMAGES,
    Condition,
    Listing,
    ListingImage,
)

PUBLISHED = ContentStatus.PUBLISHED.value

# ---------------------------------------------------------------------------
# Phone-pattern detector (Prompt 17 pitfall — the #1 scam vector).
#
# WHY two shapes: Kenyan mobiles are written as 07XX XXX XXX (national form)
# or +254 7XX XXX XXX (international form), with spaces/dashes as separators.
# The alternation matches BOTH; the anchors keep phone-shaped words inside
# longer tokens (price codes, dates like "07:00", "2019-07-15", "VIN0712…")
# from matching. NOT auto-blocking: a match queues human review only — people
# legitimately discuss phone-shaped things in public text.
_PHONE_SHAPES = re.compile(
    r"(?<!\w)\+?254[-\s]?7\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"   # +254 7XX XXX XXX
    r"|(?<!\w)0[-\s]?7\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)"       # 07XX XXX XXX
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
    .value; `q` searches titles (en + sw). REMOVED is never public."""
    listings = Listing.objects.filter(status=PUBLISHED).select_related("seller")
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
    (the flag always reflects the CURRENT public text)."""
    _assert_seller(editor, listing, "edit")
    for name in ("title", "title_sw", "description", "description_sw",
                 "price", "price_negotiable", "ward", "condition"):
        if name in fields:
            setattr(listing, name, fields[name])
    category = fields.get("category")
    if category is not None and _valid_category_or_none(category) is not None:
        listing.category = category
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
