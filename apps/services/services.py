"""Service services (Prompt 18) — one door for writes, public read API.

Consumers read through THIS module, never the model internals directly:

- visible_services(): the public skills surface (PUBLISHED only, providers
  the viewer has blocked hidden — via moderation's PUBLIC helper).
- get_visible_service(pk, for_user): single-row read policy (DRAFT /
  UNDER_REVIEW / REMOVED stay provider+staff-private). None → 404.
- service_image_count() / add_service_image(): the portfolio write door.
- reviews_for(): THE Prompt 22 cross-link hook — see its docstring for the
  exact interface reviews must satisfy when it lands.

NOTE (scope): NO phone-pattern flagger here. The 07xx/+2547xx detector was
Prompt 17's documented pitfall for goods; Prompt 18 text has no pitfall to
consolidate, so services ships without a review_flagged column. A future
report affordance should go through moderation's PUBLIC report_content —
one door for flags, not per-app rules.

Publish decision: services publish ON CREATE (like posts/listings —
"skill, rate, done"). DRAFT stays available; moderation layers
UNDER_REVIEW via its own machinery.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus
from apps.listings.models import COORDINATE_RE  # noqa: F401 — re-exported for tests
from apps.media.models import MediaRef

from .models import (
    CATEGORY_VALUES,
    MAX_IMAGES,
    MSG_TOO_MANY_IMAGES,
    RateType,
    ServiceImage,
    ServiceListing,
)

PUBLISHED = ContentStatus.PUBLISHED.value

# ---------------------------------------------------------------------------
# MERCHANTS' CONTRACT (Prompt 20 messaging lands in parallel).
#
# WHY a module constant: the route is the cross-app contract — templates,
# tests and this module all print the ONE string. Plain string, NOT reverse():
# messaging's urlconf is still settling in a parallel wave and this contract
# must not break while it evolves. `?service=` (not `?listing=`) documents the
# SEPARATE pk space: goods and services live in different tables, so
# messaging names the source table of the enquiry pk.
CHATS_URL_ROOT = "/chats/"


def enquiry_url(service_id: int) -> str:
    """THE documented enquiry contract for services (see above)."""
    return f"{CHATS_URL_ROOT}?service={service_id}"


# ---------------------------------------------------------------------------
# Prompt 22 cross-link hook — the ONE public spot reviews should target.
#
# CONTRACT for apps.reviews (lands in parallel — mock-first here):
#     apps.reviews.services.reviews_for_service(*, service_id: int,
#                                               viewer=None) -> list | None
#   A list of review objects/dicts renders on the service page; an empty
#   list is normalised to None here so the template shows one honest
#   "Reviews coming soon" state — never a blank box.
#
# WHY try-import: Prompt 22 is building CONCURRENTLY (same wave). A missing
# or mid-flight reviews app must never break the service page. The import
# failure means "not ready yet" — the honest state is None, not a crash.


def reviews_for(service: ServiceListing, *, viewer=None):
    """Reviews for a service, or None while the reviews display side is
    absent / empty. NEVER imports reviews models — only the narrow public
    service module named in the contract above.

    STATE (mid-wave): apps.reviews has landed its CREATE/SWEEP/REPUTATION
    surface (create_deal_review, publish_due_reviews, reputation_for) but
    has NO per-service DISPLAY query yet — so this import fails today
    (ImportError, name missing) and the honest page state is 'coming soon'.
    When reviews exposes a display door, only this function changes.
    """
    try:
        from apps.reviews.services import reviews_for_service  # noqa: PLC0415 — the hook is a try-import BY DESIGN
    except ImportError:
        return None  # reviews' display side not landed yet → honest slot
    try:
        found = reviews_for_service(service_id=service.pk, viewer=viewer)
    except (TypeError, LookupError, ValidationError):
        # NOTE: reviews' internal API is still settling (signature or
        # target model mid-change) — degrade to None, never break a read.
        return None
    return found or None  # empty list → None → one honest empty state


# ---------------------------------------------------------------------------
# THE REVIEWS DOOR (Prompt 22's documented contract — the side reviews CALL).
#
# apps.reviews/services.py documents: each origin app exposes ON ITS OWN
# public services module
#     review_for(deal_id, reviewer_id) -> int | None   (reviewed member id)
#     deal_label(deal_id) -> str | None                (honest deal title)
# and reviews imports NOTHING from those apps. For services, a "deal" is
# the ServiceListing itself (deal_app='services', deal_id=service.pk).
# Mirrors the landed listings door (buyer→seller; both doors honestly
# limitation-documented: no completed-booking record exists yet, so the
# party rule is "not the provider, and the service is readable to you").


def review_for(deal_id: int, reviewer_id: int) -> int | None:
    """Which member `reviewer_id` reviews over this service deal — the
    PROVIDER's id, or None (not a party / service unreadable / self-review).

    WHY None for self-review: reviews' generic-door path does not re-check
    the self case — the door is the party rule, so the provider must never
    come back as their own reviewee.
    """
    from django.contrib.auth import get_user_model

    reviewer = get_user_model().objects.filter(pk=reviewer_id).first()
    if reviewer is None:
        return None
    service = get_visible_service(deal_id, reviewer)
    if service is None:
        return None
    if reviewer.pk == service.provider_id:
        return None  # self-review refused at the door (see comment above)
    return service.provider_id


def deal_label(deal_id: int) -> str | None:
    """Honest human title for a services deal, or None (reviews then falls
    back to 'Deal #<id>' — never a fabricated name). Readable = the same
    single-row read policy as the page (DRAFT/REMOVED stay private)."""
    service = get_visible_service(deal_id, None)
    if service is None or not service.title:
        return None
    return service.title


# --- public read API (views + sibling consumers) ----------------------------


def visible_services(
    *,
    viewer=None,
    category: str | None = None,
    provider: int | None = None,
    q: str | None = None,
):
    """The skills list surface: PUBLISHED rows only, providers the viewer
    has blocked hidden (moderation PUBLIC helper), newest first.
    `category` filters on a SERVICE_CATEGORIES .value; `provider` filters
    one provider's public portfolio of services (pk int); `q` searches
    titles (en + sw). REMOVED is never public."""
    from apps.moderation.services import visible  # noqa: PLC0415 — avoids cycle risk

    services = ServiceListing.objects.filter(status=PUBLISHED).select_related(
        "provider"
    )
    services = visible(viewer, services, author_field="provider")
    if category:
        services = services.filter(category=category)
    if provider is not None:
        services = services.filter(provider__pk=provider)
    if q:
        services = services.filter(title__icontains=q)
    return services


def get_visible_service(service_id: int, for_user) -> ServiceListing | None:
    """Single-service read policy: PUBLISHED is public; DRAFT /
    UNDER_REVIEW / REMOVED stay private to the provider (staff too —
    moderation preview). None = 404 in the view — keep the policy HERE
    so every route answers the same way."""
    try:
        service = ServiceListing.objects.select_related("provider").get(
            pk=service_id
        )
    except ServiceListing.DoesNotExist:
        return None
    is_public = service.status == PUBLISHED
    is_own = getattr(for_user, "pk", None) == service.provider_id
    is_staff = getattr(for_user, "is_staff", False)
    if not is_public and not (is_own or is_staff):
        return None
    return service


# --- writes -----------------------------------------------------------------


def _assert_provider(user, service: ServiceListing, action: str) -> None:
    """Provider-only gate for service changes (staff may moderate)."""
    if user != service.provider and not getattr(user, "is_staff", False):
        raise PermissionDenied(f"Only the provider can {action} this service.")


def _valid_category_or_none(category) -> str | None:
    """Guard against an unknown category reaching storage from any path."""
    if category in CATEGORY_VALUES:
        return category
    return None


@transaction.atomic
def create_service(
    *,
    provider,
    title: str,
    description: str,
    category: str,
    ward: str,
    rate_type: str,
    rate_amount: Decimal | None = None,
    title_sw: str = "",
    description_sw: str = "",
    availability_notes: str = "",
    status: str = PUBLISHED,
) -> ServiceListing:
    """Create + publish in one tap, then check the rate-type matrix. Kept
    out of views only in the sense that ANY surface calling this gets full
    model rules via full_clean (admin/scripts/API included)."""
    service = ServiceListing(
        provider=provider,
        title=title,
        title_sw=title_sw or "",
        description=description,
        description_sw=description_sw or "",
        category=_valid_category_or_none(category),
        rate_type=rate_type or RateType.NEGOTIABLE,
        rate_amount=rate_amount,
        availability_notes=availability_notes or "",
        ward=ward,
        status=status,
    )
    service.full_clean()  # matrix + ward guard + bounds — one truth
    service.save()
    return service


@transaction.atomic
def edit_service(*, service: ServiceListing, editor, **fields) -> ServiceListing:
    """Provider-only edit. Status is deliberately NOT editable — unpublish
    is the soft-remove path (remove_service → REMOVED), same policy as
    listings. Unknown category names are dropped, not stored."""
    _assert_provider(editor, service, "edit")
    for name in (
        "title",
        "title_sw",
        "description",
        "description_sw",
        "rate_type",
        "rate_amount",
        "availability_notes",
        "ward",
    ):
        if name in fields:
            setattr(service, name, fields[name])
    category = fields.get("category")
    if category is not None and _valid_category_or_none(category) is not None:
        service.category = category
    service.full_clean()  # matrix + ward guard re-apply to the NEW content
    service.save()
    return service


def remove_service(service: ServiceListing, *, by_user) -> ServiceListing:
    """Soft delete: status REMOVED hides the service everywhere while rows
    survive for moderation/dispute evidence (never hard delete)."""
    _assert_provider(by_user, service, "remove")
    service.status = ContentStatus.REMOVED.value
    service.save(update_fields=["status", "updated_at"])
    return service


def service_image_count(service: ServiceListing) -> int:
    """The number the upload endpoint re-checks server-side before
    accepting another image (client-side checks are decoration)."""
    return service.attachments.count()


def add_service_image(
    service: ServiceListing, image: MediaRef
) -> ServiceImage:
    """Attach ONE already-processed MediaRef (Prompt 06 pipeline output) to
    the first free portfolio slot. Never accepts raw files — uploads go
    through the endpoint, which calls this AFTER its own cap check
    (defense in depth across the three cap layers)."""
    if image.owner_id != service.provider_id:
        # KDPA: you attach your own media, never someone else's upload.
        raise PermissionDenied("That image belongs to another member.")
    if service_image_count(service) >= MAX_IMAGES:
        raise ValidationError(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES})
    taken = set(service.attachments.values_list("position", flat=True))
    positions = set(range(1, MAX_IMAGES + 1)) - taken
    row = ServiceImage(service=service, image=image, position=min(positions))
    row.full_clean()  # slot legality + THE per-attachment cap on the model
    row.save()
    return row
