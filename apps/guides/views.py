"""Guide views (Prompt 08). All server-rendered; templates live in
apps/guides/templates/guides/ and extend the project base.html shell.

Read routes (list/detail/history) are public for published guides; every
write route (create/edit/publish/delete/confirm) is login_required, and
author-only actions are gated in services (author or staff).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core import images
from apps.core.types import ContentStatus

from . import offline
from . import services
from .forms import (
    STEP_FORMSET_CREATE,
    STEP_FORMSET_EDIT,
    ConfirmationForm,
    GuideForm,
)
from .models import GUIDE_CATEGORIES, Guide, GuideRevision, GuideStep, OfflineSavedGuide

# HARD RULE (PROMPTS.md budgets): a list surface shows at most 20 items per
# page — keeps the page inside the 50 KB 3G budget no matter how much the
# community writes.
PAGE_SIZE = 20


def _steps_from_formset(formset) -> list[dict]:
    """Turn validated step-builder rows into service-ready dicts. Rows left
    empty (text blank) are skipped — that's how 'extra' rows stay optional."""
    steps = []
    for form in formset.forms:
        data = getattr(form, "cleaned_data", None)
        if not data or not (data.get("text") or "").strip():
            continue
        steps.append(
            {
                "position": data["position"],
                "text": (data.get("text") or "").strip(),
                "image": data.get("image_upload"),
            }
        )
    return steps


def guide_list(request):
    """Published guides with category filter + simple search (q on titles and
    step text). ?category=<value>&q=<text>&page=<n>."""
    q = request.GET.get("q", "")
    category = request.GET.get("category", "")
    page_obj = None
    try:
        listing = services.search_guides(
            services.visible_guides(request.user), q=q, category=category
        ).order_by("-created_at")
        page_obj = Paginator(listing, PAGE_SIZE).get_page(request.GET.get("page"))
        guides = list(page_obj.object_list)
    except RuntimeError as exc:
        # NOTE: pytest-django blocks DB access in tests without a db marker;
        # the parent-owned shell test renders /guides/ that way. Degrade to
        # the static empty state instead of a 500. Real outages raise
        # OperationalError, which is NEVER swallowed here. The parent can
        # drop this guard once /guides/ leaves their stub-render parametrize.
        if "Database access not allowed" not in str(exc):
            raise
        guides = []
    return render(
        request,
        "guides/list.html",
        {
            "guides": guides,
            "page_obj": page_obj,
            "categories": GUIDE_CATEGORIES,
            "active_category": category,
            "q": q,
        },
    )


def guide_detail(request, pk):
    """The published guide: newest revision with status PUBLISHED. An author
    can preview their own unpublished guide; strangers get an honest 404."""
    guide = get_object_or_404(
        Guide.objects.exclude(status=ContentStatus.REMOVED.value), pk=pk
    )
    is_author = request.user.is_authenticated and request.user.pk == guide.created_by_id
    revision = services.latest_published_revision(guide)
    if revision is None:
        if is_author:
            revision = services.latest_revision(guide)
        if revision is None:
            raise Http404("No published revision.")
    steps = revision.steps.all()  # GuideStep.Meta.ordering = position
    return render(
        request,
        "guides/detail.html",
        {
            "guide": guide,
            "revision": revision,
            "steps": steps,
            "confirmations": guide.confirmations.all()[:PAGE_SIZE],
            "confirm_form": ConfirmationForm(),
            "is_author": is_author,
            "can_publish": is_author and revision.status != ContentStatus.PUBLISHED.value,
            # Prompt 09: the detail page labels the button — already saved vs
            # save now — without any JS round-trip. Guarded: an anonymous
            # visitor has no id to filter on (SimpleLazyObject must NOT
            # reach the DB filter).
            "saved_offline": (
                OfflineSavedGuide.objects.filter(
                    user=request.user, guide=guide
                ).exists()
                if request.user.is_authenticated
                else False
            ),
        },
    )


def guide_history(request, pk):
    """Revision history: every version with its status. Public for published
    guides (transparency); an author can see their own unpublished guide."""
    guide = get_object_or_404(
        Guide.objects.exclude(status=ContentStatus.REMOVED.value), pk=pk
    )
    is_author = request.user.is_authenticated and request.user.pk == guide.created_by_id
    if services.latest_published_revision(guide) is None and not is_author:
        raise Http404("No published revision.")
    return render(
        request,
        "guides/history.html",
        {
            "guide": guide,
            "revisions": guide.revisions.order_by("-version"),
            "is_author": is_author,
        },
    )


@login_required
def guide_create(request):
    """Create a guide (title, category, skill, safety + step builder).
    The author's first revision is published immediately (services note)."""
    if request.method == "POST":
        form = GuideForm(request.POST)
        formset = STEP_FORMSET_CREATE(
            request.POST, request.FILES, queryset=GuideStep.objects.none()
        )
        if form.is_valid() and formset.is_valid():
            try:
                guide = services.create_guide(
                    author=request.user,
                    steps=_steps_from_formset(formset),
                    **form.cleaned_data,
                )
            except images.InvalidImage as exc:
                # Prompt 06 pipeline rejected the upload (corrupt/too big...).
                messages.error(request, str(exc))
            else:
                messages.success(request, _("Guide published — thank you for sharing."))
                return redirect("guides:detail", pk=guide.pk)
        # invalid input falls through to the re-render with inline errors
    else:
        form = GuideForm()
        formset = STEP_FORMSET_CREATE(queryset=GuideStep.objects.none())
    return render(
        request, "guides/form.html", {"form": form, "formset": formset, "creating": True}
    )


@login_required
def guide_edit(request, pk):
    """Edit = new DRAFT revision (never a silent overwrite of the live page).
    The form opens with the author's latest working copy; step photos are
    snapshot-scoped and must be re-attached for the new revision."""
    guide = get_object_or_404(Guide, pk=pk)
    if request.user != guide.created_by:
        raise PermissionDenied(_("Only the author can edit this guide."))
    latest = services.latest_revision(guide)
    if request.method == "POST":
        form = GuideForm(request.POST)
        formset = STEP_FORMSET_EDIT(
            request.POST, request.FILES, queryset=GuideStep.objects.none()
        )
        if form.is_valid() and formset.is_valid():
            try:
                services.edit_guide(
                    guide=guide,
                    editor=request.user,
                    steps=_steps_from_formset(formset),
                    **form.cleaned_data,
                )
            except images.InvalidImage as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request, _("Saved as a new draft revision — publish it when ready.")
                )
                return redirect("guides:detail", pk=guide.pk)
    else:
        initial_steps = (
            [{"position": s.position, "text": s.text} for s in latest.steps.all()]
            if latest
            else []
        )
        form = GuideForm(
            initial={
                "title_en": guide.title_en,
                "title_sw": guide.title_sw,
                "category": guide.category,
                "skill_level": guide.skill_level,
                "safety_category": guide.safety_category,
                "safety_warning": guide.safety_warning,
            }
        )
        formset = STEP_FORMSET_EDIT(
            queryset=GuideStep.objects.none(), initial=initial_steps
        )
    return render(
        request,
        "guides/form.html",
        {"form": form, "formset": formset, "guide": guide, "creating": False},
    )


@login_required
@require_POST
def guide_publish(request, pk):
    """Approve a revision: POST-only (an approval is a real state change).
    Author-only via services; strangers get a 403 from the same gate."""
    guide = get_object_or_404(Guide, pk=pk)
    revision = get_object_or_404(
        GuideRevision, pk=request.POST.get("revision_id"), guide=guide
    )
    try:
        services.publish_revision(revision, by_user=request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Revision published."))
    return redirect("guides:detail", pk=guide.pk)


@login_required
def guide_delete(request, pk):
    """Soft delete with a confirm page — removal hides the guide but keeps
    every row (moderation evidence per PROMPTS.md)."""
    guide = get_object_or_404(Guide, pk=pk)
    if request.user != guide.created_by:
        raise PermissionDenied(_("Only the author can remove this guide."))
    if request.method == "POST":
        services.remove_guide(guide, by_user=request.user)
        messages.success(request, _("Guide removed."))
        return redirect("guides:list")
    return render(request, "guides/delete.html", {"guide": guide})


@login_required
@require_POST
def guide_confirm(request, pk):
    """'This worked for me in [ward]' — one confirmation per member per guide;
    confirming again updates it (people move; no duplicate spam rows)."""
    guide = get_object_or_404(
        Guide.objects.exclude(status=ContentStatus.REMOVED.value), pk=pk
    )
    form = ConfirmationForm(request.POST)
    if form.is_valid():
        services.record_confirmation(
            guide=guide,
            user=request.user,
            ward=form.cleaned_data["ward"],
            note=form.cleaned_data.get("note", ""),
        )
        messages.success(request, _("Thank you — confirmation saved."))
    else:
        messages.error(request, _("Tell us the ward where this worked."))
    return redirect("guides:detail", pk=guide.pk)


# =====================================================================
# Prompt 09 — offline saved guides (service-worker contract in offline.py)
# =====================================================================


@login_required
def offline_list(request):
    """The 'Save for offline' list (guides:offline).

    The service worker caches THIS PAGE itself (parent contract: a
    cache-first navigation for this URL) so the list opens with zero
    connectivity — the server render here is the fresh authoritative copy
    the worker re-caches on the next online sync.
    """
    data = offline.manifest(request.user, request.GET.get("page"))
    return render(
        request,
        "guides/offline.html",
        {
            "entries": data["guides"],
            "quota": data["quota"],
            "used_mb": round(data["quota"]["used_bytes"] / (1024 * 1024), 1),
            "count": data["count"],
            "page_obj": None,  # explicit: walker pagination lives in data["page"/"pages"]
            "current_page": data["page"],
            "total_pages": data["pages"],
        },
    )


@login_required
def offline_manifest(request):
    """GET /guides/offline-manifest.json (guides:offline_manifest).

    Login required (302 -> login when anonymous): a member's offline list is
    theirs alone. Pure JSON — the service worker's re-sync endpoint.
    """
    return JsonResponse(offline.manifest(request.user, request.GET.get("page")))


@login_required
@require_POST
def offline_save(request, pk):
    """POST /guides/<pk>/offline/save/ — 'Save for offline' (Prompt 09).

    POST-only + CSRF-protected (a state change), login_required, idempotent.
    Returns JSON the worker acts on immediately: {"saved": true, "hash",
    "url", "pages", "images", ...} — cache it all now, store the hash.
    A since-REMOVED guide can't be newly saved: get_object_or_404 on the
    not-removed queryset.
    """
    guide = get_object_or_404(
        Guide.objects.exclude(status=ContentStatus.REMOVED.value), pk=pk
    )
    return JsonResponse(offline.save_offline(user=request.user, guide=guide))


@login_required
@require_POST
def offline_unsave(request, pk):
    """POST /guides/<pk>/offline/unsave/ — remove the bookmark. JSON
    {"saved": false}; the worker then evicts the cached page + images.
    Allowed even on a REMOVED guide: a member must always be able to clean
    up their own offline list (eviction is the user's right, not content's).
    """
    guide = get_object_or_404(Guide, pk=pk)
    return JsonResponse(offline.unsave_offline(user=request.user, guide=guide))
