"""Guide services (Prompt 08) — one door for every write + list/search reads.

Revision/approval flow (the documented Prompt 08 decision):
1. create_guide:      Guide + revision v1 → PUBLISHED at once. WHY: the
                      author is approving their own first version; community
                      moderation (Prompt 15) layers UNDER_REVIEW on top later.
2. edit_guide:        EVERY edit creates a NEW revision, status DRAFT. The
                      public page keeps showing the newest PUBLISHED revision,
                      so an edit can never silently change a live page
                      (revision-safe acceptance).
3. publish_revision:  DRAFT or UNDER_REVIEW → PUBLISHED, gated to the author
                      (or staff). UNDER_REVIEW is the enum state Prompt 15's
                      moderation queue will use; it must not block publishing.
4. remove_guide:      Guide.status = REMOVED (soft delete) — rows survive for
                      dispute evidence per PROMPTS.md moderation rules.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory
from apps.media.services import save_upload

from .models import Guide, GuideConfirmation, GuideRevision, GuideStep

# A revision can be approved from either of these states. NOT from REMOVED
# (removed content stays removed — moderation evidence, PROMPTS.md 15).
PUBLISHABLE_STATUSES = (
    ContentStatus.DRAFT.value,
    ContentStatus.UNDER_REVIEW.value,
)


def visible_guides(for_user):
    """List-surface queryset. Everyone sees PUBLISHED guides; an author also
    sees their own work-in-progress. REMOVED is never listed (soft delete)."""
    base = Guide.objects.exclude(status=ContentStatus.REMOVED.value)
    if getattr(for_user, "is_authenticated", False):
        return base.filter(
            Q(status=ContentStatus.PUBLISHED.value) | Q(created_by=for_user)
        )
    return base.filter(status=ContentStatus.PUBLISHED.value)


def search_guides(queryset, *, q: str = "", category: str = ""):
    """Category filter + simple title/step-text search (Prompt 08 contract).

    WHY step text matches ANY revision: cheap, and a member may have read a
    wording an author later edited. Prompt 14 replaces this with a maintained
    SearchVector + trigram matching.
    """
    if category:
        queryset = queryset.filter(category=category)
    term = (q or "").strip()
    if term:
        queryset = queryset.filter(
            Q(title_en__icontains=term)
            | Q(title_sw__icontains=term)
            | Q(revisions__steps__text__icontains=term)
        ).distinct()
    return queryset


def latest_published_revision(guide: Guide) -> GuideRevision | None:
    """'Latest approved revision' — the newest snapshot with status PUBLISHED."""
    return (
        guide.revisions.filter(status=ContentStatus.PUBLISHED.value)
        .order_by("-version")
        .first()
    )


def latest_revision(guide: Guide) -> GuideRevision | None:
    """Newest snapshot regardless of status (author previews; edit prefill)."""
    return guide.revisions.order_by("-version").first()


def _start_revision(
    guide: Guide, *, title_en: str, title_sw: str,
    safety_category: str, safety_warning: str,
) -> GuideRevision:
    version = (guide.revisions.aggregate(n=Max("version"))["n"] or 0) + 1
    revision = GuideRevision(
        guide=guide,
        version=version,
        title_en=title_en,
        title_sw=title_sw or "",
        safety_category=safety_category,
        safety_warning=safety_warning or "",
        status=ContentStatus.DRAFT.value,
    )
    revision.full_clean()  # model clean() — the safety-warning gate
    revision.save()
    return revision


def _write_steps(revision: GuideRevision, steps, *, owner) -> int:
    """Write one revision's step snapshot. `steps` entries carry position,
    text and an optional uploaded image file — which MUST flow through the
    Prompt 06 pipeline (save_upload: EXIF strip + WebP variants); no raw
    file writes and no re-used client paths, ever."""
    seen_positions: set[int] = set()
    kept = []
    for entry in sorted(steps, key=lambda s: s.get("position") or 0):
        text = (entry.get("text") or "").strip()
        if not text:
            continue  # untouched extra rows in the step builder form
        position = entry["position"]
        if position in seen_positions:
            raise ValidationError(
                _("Two steps share position %(n)s.") % {"n": position}
            )
        seen_positions.add(position)
        kept.append((position, text, entry.get("image")))
    revision.steps.all().delete()  # a snapshot's steps are rewritten wholesale
    for position, text, upload in kept:
        ref = None
        if upload is not None:
            ref = save_upload(file_obj=upload, owner=owner, purpose="guide")
        step = GuideStep(revision=revision, position=position, text=text, image=ref)
        step.full_clean()  # length/position guards live on the model clean()
        step.save()
    return len(kept)


def _assert_author(user, guide: Guide, action: str) -> None:
    """Author-only gate for content changes (staff may moderate)."""
    if user != guide.created_by and not user.is_staff:
        raise PermissionDenied(f"Only the author can {action} this guide.")


@transaction.atomic
def create_guide(
    *, author, title_en: str, category: str, skill_level: str,
    title_sw: str = "", safety_category: str = SafetyCategory.NONE.value,
    safety_warning: str = "", steps=(),
) -> Guide:
    """Create the Guide, its first revision and steps, then publish v1.
    Atomic: a bad step image or validation failure leaves no partial rows."""
    guide = Guide(
        title_en=title_en,
        title_sw=title_sw or "",
        category=category,
        skill_level=skill_level,
        safety_category=safety_category,
        safety_warning=safety_warning or "",
        created_by=author,
        status=ContentStatus.DRAFT.value,
    )
    guide.full_clean()  # model clean() — safety gate, not just the form
    guide.save()
    revision = _start_revision(
        guide,
        title_en=guide.title_en,
        title_sw=guide.title_sw,
        safety_category=guide.safety_category,
        safety_warning=guide.safety_warning,
    )
    _write_steps(revision, steps, owner=author)
    publish_revision(revision, by_user=author)  # author approves v1 (see docstring)
    return guide


@transaction.atomic
def edit_guide(
    *, guide: Guide, editor, title_en: str, category: str, skill_level: str,
    title_sw: str = "", safety_category: str = SafetyCategory.NONE.value,
    safety_warning: str = "", steps=(),
) -> GuideRevision:
    """Every edit = new DRAFT revision (version max+1) + fresh steps snapshot.
    Updates the Guide working copy so the next edit form opens with the
    author's latest intentions, while published pages stay untouched."""
    _assert_author(editor, guide, "edit")
    guide.title_en = title_en
    guide.title_sw = title_sw or ""
    guide.category = category
    guide.skill_level = skill_level
    guide.safety_category = safety_category
    guide.safety_warning = safety_warning or ""
    guide.full_clean()
    guide.save()
    revision = _start_revision(
        guide,
        title_en=guide.title_en,
        title_sw=guide.title_sw,
        safety_category=guide.safety_category,
        safety_warning=guide.safety_warning,
    )
    _write_steps(revision, steps, owner=editor)
    return revision


def publish_revision(revision: GuideRevision, *, by_user) -> GuideRevision:
    """The approval gate: author (or staff) flips DRAFT/UNDER_REVIEW to
    PUBLISHED. Already-published and removed revisions are refused."""
    guide = revision.guide
    _assert_author(by_user, guide, "publish a revision of")
    if revision.status not in PUBLISHABLE_STATUSES:
        raise ValidationError(_("Only a draft or under-review revision can be published."))
    revision.status = ContentStatus.PUBLISHED.value
    revision.published_at = timezone.now()
    revision.save(update_fields=["status", "published_at", "updated_at"])
    if guide.status != ContentStatus.PUBLISHED.value:
        guide.status = ContentStatus.PUBLISHED.value
        guide.save(update_fields=["status", "updated_at"])
    return revision


def submit_for_review(revision: GuideRevision) -> GuideRevision:
    """Move a draft into the UNDER_REVIEW state (Prompt 15 moderation queue).
    Publishing still accepts UNDER_REVIEW — review must never become a dead end."""
    if revision.status != ContentStatus.DRAFT.value:
        raise ValidationError(_("Only a draft revision can be submitted for review."))
    revision.status = ContentStatus.UNDER_REVIEW.value
    revision.save(update_fields=["status", "updated_at"])
    return revision


def remove_guide(guide: Guide, *, by_user) -> Guide:
    """Soft delete: status REMOVED hides the guide everywhere while the rows
    remain for moderation/dispute evidence (PROMPTS.md — never hard delete)."""
    _assert_author(by_user, guide, "remove")
    guide.status = ContentStatus.REMOVED.value
    guide.save(update_fields=["status", "updated_at"])
    return guide


def record_confirmation(
    *, guide: Guide, user, ward: str, note: str = ""
) -> GuideConfirmation:
    """'This worked for me in [ward]'. One row per (guide, user): confirming
    again updates the ward/note instead of piling up duplicates."""
    ward = (ward or "").strip()
    if not ward:
        raise ValidationError(_("Tell us the ward where this worked."))
    obj, created = GuideConfirmation.objects.update_or_create(
        guide=guide,
        user=user,
        defaults={"ward": ward[:64], "note": (note or "").strip()},
    )
    return obj
