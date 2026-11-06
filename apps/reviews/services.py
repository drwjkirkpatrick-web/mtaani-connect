"""Reviews services (Prompt 22) — one door: creation, the sweep, reputation.

PUBLIC API for other modules (parent wires cron/schedules + profile display):

    from apps.reviews import services

    services.create_deal_review(*, reviewer, deal_app, deal_id, stars,
                                text="", dispute_note="")   -> DealReview (DRAFT)
    services.publish_due_reviews(now=None)                       -> list[DealReview]
    services.reputation_for(user, now=None)                      -> display dict
    services.deal_label(deal_app, deal_id)                       -> honest title

THE DEAL-REF CONTRACT (generic "reviewable deal" — for barter Prompt 21 and
disputes Prompt 23 to rely on):
    A review points at a deal with (deal_app, deal_id) where deal_app is in
    models.ALLOWED_ORIGIN_APPS = ('listings', 'services', 'barter'). Reviews
    import NOTHING from those apps; each origin plugs in by exposing on ITS
    OWN public `services` module:
        review_for(deal_id, reviewer_id) -> int | None
            the member id `reviewer_id` ends up reviewing on this deal
            (None = not a deal party / deal unreadable); AND
        deal_label(deal_id) -> str | None
            an honest human title for the deal (None lets reviews fall back
            to 'Deal #<id>').
    Until an origin lands its door, reviews for it are honestly refused
    (MSG_DEAL_NOT_READABLE) — reviews never guess a deal's parties. The
    'listings' door is implemented HERE directly on listings' public read
    door (buyer reviews the seller; see the listings decision below).

DECISIONS (documented for reviewers):
- ONE PIPELINE, NO IMMEDIATE PUBLISH: listing-level reviews follow the same
  DRAFT→sweep path as barter pairs. The spec's "no retaliation window" is an
  acceptance, and an instantly-published buyer's 1-star is precisely a
  retaliation window (the seller would see it before they can rate back).
  Publishing only on BOTH-in or 14-days satisfies the spec for every deal
  shape. Listing deals carry no mutual obligation (only the buyer leaves
  one, per data available: listings has no buyer identity); if the seller
  also rates back, the door + unique constraint make the pair sweep-pairable.
- SWEEP, NOT TRIGGER (spec pitfall): a submit-time trigger can't see the
  counterpart's future row; publication is a periodic pass over held rows —
  pairs publish in one tick when both exist, lone rows when 14 days pass.
- 1-STAR GATE: for now every 1-star must carry a dispute_note (≥20 chars).
  When apps.disputes (Prompt 23) lands, the gate tightens to "a dispute is
  OPEN on the deal" via one helper (models._dispute_is_open) — the field and
  minimum length stay, the condition is the only change.
- REVIEWS ARE IMMUTABLE: no edit/deletion routes; the sweep is the only
  state change, moderation may REMOVE a row (status lifecycle), and
  published_at is set by the sweep alone (MSG_NOT_HELD guards hand-edits).
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus

from .models import (
    ALLOWED_ORIGIN_APPS,
    DealReview,
    MSG_ALREADY_REVIEWED,
    MSG_DEAL_NOT_READABLE,
    MSG_LOGIN,
    MSG_ORIGIN_NOT_ALLOWED,
    MSG_SELF_REVIEW,
    MSG_STAR_BOUNDS,
    UNILATERAL_PUBLISH_DAYS,
)

DRAFT = ContentStatus.DRAFT.value
PUBLISHED = ContentStatus.PUBLISHED.value

# Trust's GENERAL bucket (Prompt 16 public contract: the scorer's category
# argument) — the literal is the documented cross-app vocabulary, so reviews
# keep zero trust-model imports.
TRUST_REVIEW_CATEGORY = "GENERAL"

# Rollup window + weights — ONE constant, one consumer below (trust uses the
# same 90-day convention; DEAL weight > TRUST weight because deal reviews are
# transaction-backed evidence while trust votes are social niceness).
WINDOW_DAYS = 90
DEAL_WEIGHT = 0.6
TRUST_WEIGHT = 0.4


def _now(now):
    """The evaluation clock: injected `now` wins, otherwise the real clock."""
    return timezone.now() if now is None else now


# ---------------------------------------------------------------------------
# Deal-ref resolution (per-origin doors, never internals)
# ---------------------------------------------------------------------------


def _origin_services(deal_app: str):
    """The origin app's services module, or None when not landed/importable."""
    from importlib import import_module

    try:
        return import_module(f"apps.{deal_app}.services")
    except ImportError:
        return None


def _resolve_subject(deal_app, deal_id, reviewer) -> tuple[str, int | None]:
    """(kind, reviewed_id) for the reviewer on this deal.

    kind is 'ok' (review `reviewed_id`), 'self' (reviewer IS the counterpart
    — the self-review case) or 'absent' (deal unreadable / not a party).
    """
    if deal_app == "listings":
        # listings' public door only; seller = reviewed, reviewer must be a
        # buyer-side member. NOTE: listings has NO buyer identity yet, so the
        # party check is "not the seller, listing visible to you" — the
        # limitation is the listings data model's, documented (Prompt 24 may
        # add deal records; only this function changes then).
        from apps.listings.services import get_visible_listing

        listing = get_visible_listing(deal_id, reviewer)
        if listing is None:
            return ("absent", None)
        if reviewer.pk == listing.seller_id:
            return ("self", listing.seller_id)
        return ("ok", listing.seller_id)

    # Contract doors for concurrent/future origin apps (barter Prompt 21,
    # services Prompt 18): `review_for(deal_id, reviewer_id) -> int | None`.
    module = _origin_services(deal_app)
    door = getattr(module, "review_for", None) if module is not None else None
    if callable(door):
        try:
            subject_id = door(deal_id, reviewer.pk)
        except Exception:  # noqa: BLE001 — a broken door is honest absence
            return ("absent", None)
        if subject_id:
            return ("ok", int(subject_id))
        return ("absent", None)
    return ("absent", None)


def _validate_subject(*, reviewer, deal_app, deal_id) -> int:
    """Shared gate: login + allow-list + the origin's party rule. Returns
    the reviewed member's id or raises the honest ValidationError."""
    if reviewer is None or not getattr(reviewer, "is_authenticated", False):
        raise ValidationError(MSG_LOGIN)
    if deal_app not in ALLOWED_ORIGIN_APPS:
        raise ValidationError(MSG_ORIGIN_NOT_ALLOWED)
    kind, subject_id = _resolve_subject(deal_app, deal_id, reviewer)
    if kind == "self":
        raise ValidationError(MSG_SELF_REVIEW)
    if kind != "ok" or subject_id is None:
        raise ValidationError(MSG_DEAL_NOT_READABLE)
    return subject_id


def reviewable_deal(*, user, deal_app: str, deal_id: int) -> tuple[bool, str]:
    """Read-only pre-flight for form surfaces: (may I review here?, honest
    reason when not). Same gates as create, writes nothing — views never
    re-implement party rules."""
    try:
        _validate_subject(reviewer=user, deal_app=deal_app, deal_id=deal_id)
    except ValidationError as exc:
        messages_list = getattr(exc, "messages", None) or [str(exc)]
        return False, " ".join(messages_list)
    return True, ""


def star_flags(value: int | float) -> list[bool]:
    """Five-slot boolean row for badge-ish templates (round-half-up on the
    reputation score; exact for star values). One helper, one row-format."""
    filled = int(float(value) + 0.5)
    return [filled >= position for position in range(1, 6)]


# ---------------------------------------------------------------------------
# Creation (always creates a HELD row — never publishes)
# ---------------------------------------------------------------------------


@transaction.atomic
def create_deal_review(
    *,
    reviewer,
    deal_app: str,
    deal_id: int,
    stars: int,
    text: str = "",
    dispute_note: str = "",
) -> DealReview:
    """Create one HELD review (ContentStatus DRAFT) of the deal counterpart.

    Gates: reviewer logged in; deal_app allow-listed; the origin's door says
    the reviewer is a deal party (never the reviewed member themself); one
    row per (deal, reviewer); stars 1–5; 1-star carries a dispute_note
    (models' bound, service re-checks). NEVER publishes — see the sweep.
    Reviewed identity comes from the door only: `reviewed` is not a caller
    argument, so a member cannot aim a review at an unrelated user.
    """
    subject_id = _validate_subject(
        reviewer=reviewer, deal_app=deal_app, deal_id=deal_id
    )
    try:
        deal_id = int(deal_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError(MSG_DEAL_NOT_READABLE) from exc
    if deal_id <= 0:
        raise ValidationError(MSG_DEAL_NOT_READABLE)
    try:
        stars = int(stars)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"stars": MSG_STAR_BOUNDS}) from exc
    if not 1 <= stars <= 5:
        raise ValidationError({"stars": MSG_STAR_BOUNDS})  # before clean, too
    review = DealReview(
        reviewer=reviewer,
        reviewed_id=subject_id,
        deal_app=deal_app,
        deal_id=deal_id,
        stars=stars,
        text=(text or "").strip(),
        dispute_note=(dispute_note or "").strip(),
        status=DRAFT,
    )
    if DealReview.objects.filter(
        deal_app=review.deal_app, deal_id=review.deal_id, reviewer=reviewer
    ).exists():
        raise ValidationError(MSG_ALREADY_REVIEWED)
    review.full_clean()  # stars bounds, text cap, 1-star note, allow-list
    try:
        review.save()
    except IntegrityError:
        # Race with a sibling submit — the DB constraint is the arbiter.
        raise ValidationError(MSG_ALREADY_REVIEWED) from None
    return review


# ---------------------------------------------------------------------------
# The two-phase sweep (the ONLY path from DRAFT to PUBLISHED)
# ---------------------------------------------------------------------------


def _publish_row(review: DealReview, when) -> DealReview:
    """Move one row to PUBLISHED with the sweep's tick as published_at.
    save(update_fields=...) skips clean() on purpose: the held→published
    transition is exactly what clean() must never refuse from a hand path."""
    review.status = PUBLISHED
    review.published_at = when
    review.save(update_fields=["status", "published_at", "updated_at"])
    return review


@transaction.atomic
def publish_due_reviews(*, now=None) -> list[DealReview]:
    """The two-phase publication sweep (spec pitfall: a job, not a trigger).

    Phase 1 — PAIRS: on any deal where the counterpart review exists too,
    both rows publish in the same tick (identical published_at: 'both
    submitted → simultaneous'). Pair = A reviews B and B reviews A.
    Phase 2 — UNILATERAL: any remaining DRAFT older than 14 days (boundary
    INCLUSIVE: a row exactly (created_at + 14d) old publishes this pass) —
    the no-retaliation window closes with or without a counterpart.

    Determinism contract: held rows are read ordered by (created_at, pk) and
    deals grouped by sorted (deal_app, deal_id), so an injected clock
    replays identically (same rows + same now → same result). Idempotent:
    PUBLISHED rows are never re-selected or re-stamped.
    """
    now = _now(now)
    held = list(
        DealReview.objects.filter(status=DRAFT).order_by("created_at", "pk")
    )
    by_deal: dict[tuple[str, int], list[DealReview]] = {}
    for row in held:
        by_deal.setdefault((row.deal_app, row.deal_id), []).append(row)

    published: list[DealReview] = []
    stamped: set[int] = set()
    # Phase 1 over deals in sorted key order — pairs before their own 14-day
    # clock matters, so a same-tick pair is visible to both parties at once.
    for key in sorted(by_deal):
        rows = by_deal[key]
        for index, first in enumerate(rows):
            if first.pk in stamped:
                continue
            for second in rows[index + 1 :]:
                if second.pk in stamped:
                    continue
                if first.reviewer_id == second.reviewed_id and (
                    second.reviewer_id == first.reviewed_id
                ):
                    published.append(_publish_row(first, now))
                    published.append(_publish_row(second, now))
                    stamped.update({first.pk, second.pk})
                    break

    # Phase 2 — the 14-day unilateral release (boundary day inclusive).
    window_close = now - timedelta(days=UNILATERAL_PUBLISH_DAYS)
    for row in held:
        if row.pk in stamped:
            continue
        if row.created_at <= window_close:
            published.append(_publish_row(row, now))
            stamped.add(row.pk)
    return published


# ---------------------------------------------------------------------------
# Deal label (honest titles, no fabrication)
# ---------------------------------------------------------------------------


def deal_label(deal_app: str, deal_id: int) -> str:
    """An honest human-readable deal title for display surfaces.

    Tries the origin app's public door; a door that is absent, broken, or
    points at a deal with no public record (e.g. a REMOVED listing) yields
    'Deal #<id>' — reviews NEVER fabricate a title they cannot read.
    """
    fallback = _("Deal #%(id)s") % {"id": deal_id}
    if deal_app == "listings":
        try:
            from apps.listings.services import get_visible_listing

            listing = get_visible_listing(deal_id, None)
            if listing is not None and listing.title:
                return listing.title
        except Exception:  # noqa: BLE001 — a label must never break a page
            return fallback
        return fallback
    module = _origin_services(deal_app)
    door = getattr(module, "deal_label", None) if module is not None else None
    if callable(door):
        try:
            label = door(deal_id)
        except Exception:  # noqa: BLE001 — a broken door is honest absence
            return fallback
        if label:
            return str(label)
    return fallback


# ---------------------------------------------------------------------------
# Per-user reputation rollup (Prompt 22 acceptance: feeds profile display)
# ---------------------------------------------------------------------------


def _empty_reputation() -> dict:
    """The display dict for a member with no signals (never None — every
    template can render it without branches) — NOTE/WHY honest zeros."""
    return {
        "user_id": None,
        "score": 0.0,
        "scale": 5,
        "stars_filled": 0,
        "star_flags": [False] * 5,
        "deal_reviews": {
            "lifetime": 0,
            "last90": 0,
            "avg_stars": 0.0,
            "component_out_of_five": 0.0,
        },
        "trust": {
            "category": TRUST_REVIEW_CATEGORY,
            "score_zero_to_one": 0.0,
            "component_out_of_five": 0.0,
        },
        "weights": {"deal": DEAL_WEIGHT, "trust": TRUST_WEIGHT},
        "formula": _(
            "0.6 × mean deal stars (0–5) + 0.4 × 5 × trust usefulness (0–1)"
        ),
    }


def reputation_for(user, *, now=None) -> dict:
    """THE reputation rollup (deterministic, injectable clock).

    FORMULA (documented, explainable — see trust's usefulness_scorer for its
    0..1 normalization): with avg = mean of PUBLISHED deal-review stars
    RECEIVED (0.0 with no reviews; lifetime average — the last90 figures are
    reported alongside for recency trend),
        reputation (0–5) = 0.6·avg + 0.4·(5 × trust_usefulness)

    Weights: deals 0.6 / trust 0.4 (deal reviews are transaction-backed).
    Every component stays 0–5-scaled for templates; the dict is display-
    shaped (no model rows), rows read ordered by (created_at, pk) and only
    PUBLISHED reviews count — held rows never leak into reputation.
    """
    from apps.trust.services import usefulness_scorer  # PUBLIC door only

    rollup = _empty_reputation()
    rollup["user_id"] = getattr(user, "pk", None)
    now = _now(now)
    rows = list(
        DealReview.objects.filter(reviewed=user, status=PUBLISHED).order_by(
            "created_at", "pk"
        )
    )
    window_start = now - timedelta(days=WINDOW_DAYS)
    recent = [row for row in rows if row.created_at >= window_start]
    lifetime = len(rows)
    avg = round(sum(row.stars for row in rows) / lifetime, 4) if lifetime else 0.0
    avg_recent = (
        round(sum(row.stars for row in recent) / len(recent), 4)
        if recent
        else 0.0
    )
    deal_component = round(avg, 4)  # already 0–5 (avg of 1–5 star reviews)
    trust_0_1 = usefulness_scorer(user, TRUST_REVIEW_CATEGORY, now=now)
    trust_component = round(5.0 * float(trust_0_1), 4)
    score = round(
        DEAL_WEIGHT * deal_component + TRUST_WEIGHT * trust_component, 2
    )
    # Round-half-up star row for the badge-ish partial (display convenience,
    # computed from the same score the page prints — no second truth source).
    rollup["score"] = score
    rollup["stars_filled"] = int(score + 0.5)
    rollup["star_flags"] = star_flags(score)
    rollup["deal_reviews"] = {
        "lifetime": lifetime,
        "last90": len(recent),
        "avg_stars": avg,
        "avg_stars_last90": avg_recent,
        "component_out_of_five": deal_component,
    }
    rollup["trust"] = {
        "category": TRUST_REVIEW_CATEGORY,
        "score_zero_to_one": float(trust_0_1),
        "component_out_of_five": trust_component,
    }
    return rollup


def reviews_for_service(*, service_id, viewer=None) -> list:
    """DISPLAY door (parent wiring, Prompt 22) probed by apps/services'
    reviews_for hook: the PUBLISHED reviews of one services.ServiceListing.

    viewer param accepted for contract symmetry (published reviews are
    public — the held/'DRAFT' rows belong to the sweep only). Immutable
    ordering: newest published first, pk as the stable tie-break.
    """
    return list(
        DealReview.objects.filter(
            deal_app="services",
            deal_id=int(service_id),
            status=ContentStatus.PUBLISHED.value,
        )
        .select_related("reviewer")
        .order_by("-published_at", "-pk")
    )
