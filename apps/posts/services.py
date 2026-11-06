"""Post services (Prompt 10) — one door for writes, public read API for the
feed, and the server-side reference-preview helper.

Decisions documented for Prompt 11 (Utility Feed Ranking), which is a
READ-ONLY consumer of this module — it must never import the models directly:
- visible_posts(): PUBLISHED posts only, newest-first (recency within a feed
  is the feed's business; this list is the honest chronological surface).
- educational_posts(): ?tag=educational filter lives here so the query never
  drifts between the list view and future consumers.
- get_visible_post(pk, for_user): single-post read with draft/REMOVED policy
  (author + staff only).

Publish decision: posts publish ON CREATE for members (word-of-mouth must be
one tap: "type, done"). DRAFT exists for future workflows and Prompt 15
moderation layers UNDER_REVIEW — mirrors apps.guides.create_guide.
"""
from __future__ import annotations

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus
from apps.guides.services import latest_published_revision, visible_guides
from apps.listings.services import get_visible_listing  # Prompt 17 consolidation
from apps.media.models import MediaRef

from .models import MAX_IMAGES, MSG_TOO_MANY_IMAGES, Post, PostImage

PUBLISHED = ContentStatus.PUBLISHED.value


# --- public read API (feed + views) ------------------------------------------


def visible_posts(*, educational: bool | None = None, limit: int | None = None):
    """Public chronological list surface. Everyone sees PUBLISHED posts only.

    `educational=True` is the ?tag=educational filter contract; None = no tag
    filter. Feed (Prompt 11) calls this with its own ordering downstream —
    never with hidden denormalised state.
    """
    qs = Post.objects.filter(status=PUBLISHED).select_related(
        "author", "ref_guide"
    ).prefetch_related("attachments__image")
    if educational is True:
        qs = qs.filter(educational=True)
    if limit:
        qs = qs[:limit]
    return qs


def get_visible_post(post_id: int, for_user) -> Post | None:
    """Single-post read policy: PUBLISHED is public; DRAFT and REMOVED stay
    private to their author (staff too, moderation preview). None = 404 in
    the view — keep the policy HERE so every route answers the same way."""
    try:
        post = Post.objects.select_related("author", "ref_guide").get(pk=post_id)
    except Post.DoesNotExist:
        return None
    is_public = post.status == PUBLISHED
    is_own = getattr(for_user, "pk", None) == post.author_id
    is_staff = getattr(for_user, "is_staff", False)
    if not is_public and not (is_own or is_staff):
        return None
    return post


# --- writes -------------------------------------------------------------------


def _assert_author(user, post: Post, action: str) -> None:
    """Author-only gate for content changes (staff may moderate)."""
    if user != post.author and not getattr(user, "is_staff", False):
        raise PermissionDenied(f"Only the author can {action} this post.")


@transaction.atomic
def create_post(
    *, author, text: str, educational: bool = False,
    ref_guide=None, ref_listing: int | None = None,
) -> Post:
    """Create + publish in one tap. full_clean keeps text length and any
    model rule enforced no matter which surface calls in."""
    post = Post(
        author=author,
        text=text,
        educational=educational,
        status=PUBLISHED,
        ref_guide=ref_guide,
        ref_listing=ref_listing,
    )
    post.full_clean()
    post.save()
    return post


@transaction.atomic
def edit_post(
    *, post: Post, editor, text: str, educational: bool, ref_guide
) -> Post:
    """Author-only edit. Status is deliberately NOT editable here — unpublish
    is the moderation/soft-remove path (remove_post → REMOVED)."""
    _assert_author(editor, post, "edit")
    post.text = text
    post.educational = educational
    post.ref_guide = ref_guide
    post.full_clean()
    post.save()
    return post


def remove_post(post: Post, *, by_user) -> Post:
    """Soft delete: status REMOVED hides the post everywhere while rows
    survive for moderation/dispute evidence (PROMPTS.md — never hard delete)."""
    _assert_author(by_user, post, "remove")
    post.status = ContentStatus.REMOVED.value
    post.save(update_fields=["status", "updated_at"])
    return post


def add_image(post: Post, image: MediaRef) -> PostImage:
    """Attach ONE already-processed MediaRef (Prompt 06 pipeline output) to
    the first free slot. Never accepts raw files — uploads go through the
    endpoint, which must call this AFTER its own cap check (defense in depth)."""
    if image.owner_id != post.author_id:
        # KDPA: you attach your own media, never someone else's upload.
        raise PermissionDenied("That image belongs to another member.")
    taken = set(post.attachments.values_list("position", flat=True))
    if len(taken) >= MAX_IMAGES:
        raise ValidationError(MSG_TOO_MANY_IMAGES % {"n": MAX_IMAGES})
    positions = set(range(1, MAX_IMAGES + 1)) - taken
    row = PostImage(post=post, image=image, position=min(positions))
    row.full_clean()  # cap + slot legality live on the model clean()
    row.save()
    return row


def post_image_count(post: Post) -> int:
    """The number the upload endpoint re-checks server-side before accepting
    another image (client-side checks are decoration — PROMPTS.md pitfall)."""
    return post.attachments.count()


# --- reference preview (server-side rendering of typed refs) ------------------


def reference_preview(*, ref_guide, ref_listing, viewer) -> dict | None:
    """Build the preview-card data for a post's typed references.

    Guide refs read the guides app through its PUBLIC functions only
    (visible_guides → viewer-appropriate row; latest_published_revision → the
    displayed title). If the guide is no longer visible to this viewer we say
    so ("Guide no longer available") instead of faking a title.

    Listing refs: Prompt 17 does not exist yet — the honest NO_DATA state
    ("Listing preview coming soon"), never invented data. The id travels only
    inside our own state; nothing is guessed from it.
    """
    if ref_guide_id := getattr(ref_guide, "pk", None):
        try:
            guide = visible_guides(viewer).get(pk=ref_guide_id)
        except ObjectDoesNotExist:  # not visible to this viewer → honesty state
            return {"kind": "guide", "state": "UNAVAILABLE", "title": "", "url": ""}
        rev = latest_published_revision(guide)
        title = rev.display_title if rev else guide.display_title
        return {
            "kind": "guide",
            "state": "OK",
            "title": title,
            "url": reverse("guides:detail", args=[guide.pk]),
        }
    if ref_listing:
        # Prompt 17 consolidation: listings are LIVE now. The PUBLIC read
        # door applies its single visibility policy (PUBLISHED is public;
        # drafts/removed resolve None). No visible listing → the same
        # honesty state as guides ("no longer available"), never invented
        # data — the id travels only inside our own state.
        listing = get_visible_listing(ref_listing, viewer)
        if listing is None:
            return {"kind": "listing", "state": "UNAVAILABLE", "title": "", "url": ""}
        title = str(listing.title or listing.title_sw or "").strip()
        return {
            "kind": "listing",
            "state": "OK",
            "title": title or "Listing #%d" % ref_listing,
            "url": f"/market/{listing.pk}/",
        }
    return None
