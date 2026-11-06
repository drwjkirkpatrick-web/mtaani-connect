"""Moderation views (Prompt 15).

Route surface (namespace `moderation`, POST-only where actions change state):
- report_create  member tool (login)   — rate-limited 10/hour/user
- queue          moderator tool        — role-gated 403 for everyone else
- resolve        moderator tool        — review/dismiss/remove/restore
- draft, confirm moderator tools       — two-actor suspension gate
- block, unblock member tool           — visibility, reversible

Errors on action POSTs are plain localized messages (400/429), never crashes —
these endpoints are called from embedded forms across sibling apps.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from . import services
from .forms import ReportForm
from .models import Report, SuspensionDraft

QUEUE_PAGE_SIZE = 20


def _safe_next(request, default: str = "/") -> str:
    """Redirect only to local URLs — the report endpoint is POSTed from many
    pages, so an open-redirect here would be a site-wide hole."""
    candidate = request.POST.get("next") or request.GET.get("next") or default
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return default


def _bad_request(exc: ValidationError) -> HttpResponseBadRequest:
    return HttpResponseBadRequest(" ".join(exc.messages), content_type="text/plain; charset=utf-8")


def _rate_limited(exc: ValidationError) -> HttpResponse:
    return HttpResponse(
        str(exc.args[0] if exc.messages else exc), status=429,
        content_type="text/plain; charset=utf-8",
    )


def require_moderator(view):
    """Role gate: is_moderator is the single source (services.is_moderator);
    everyone else gets a hard 403 (roles enforced, acceptance contract)."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not services.is_moderator(request.user):
            raise PermissionDenied(_("Moderator role required."))
        return view(request, *args, **kwargs)

    return wrapped


@login_required
@require_http_methods(["GET", "POST"])
def report_create(request):
    """Member tool: report any allowed target. 10 reports/hour/user.

    WHY POST-only actions: no state change ever happens on GET (bots, link
    prefetchers and tab-savers must not create reports or fire moderation).
    Sibling templates can post straight here with hidden fields.
    """
    if request.method == "GET":
        return render(
            request, "moderation/report_form.html",
            {"form": ReportForm()},
        )
    form = ReportForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest(" ".join(form.non_field_errors() or [_("Invalid report.")]), content_type="text/plain; charset=utf-8")
    try:
        services.report_content(
            reporter=request.user,
            target=form.target,
            reason=form.cleaned_data["reason"],
            note=form.cleaned_data.get("note", ""),
        )
    except services.RateLimitError as exc:
        return _rate_limited(exc)
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect(_safe_next(request))


@login_required
@require_moderator
def queue(request):
    """Moderation queue: open/under-review reports + suspension drafts."""
    page_obj = Paginator(services.queue_reports(), QUEUE_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    return render(
        request,
        "moderation/queue.html",
        {
            "page_obj": page_obj,
            "reports": page_obj.object_list,
            "drafts": services.pending_suspensions(),
        },
    )


@require_moderator
@require_http_methods(["POST"])
def report_resolve(request, pk: int):
    """Act on one queued report: decision ∈ review|dismiss|remove|restore."""
    report = get_object_or_404(Report, pk=pk)
    decision = request.POST.get("decision", "")
    try:
        services.settle_report(
            report,
            moderator=request.user,
            decision=decision,
            decision_note=request.POST.get("decision_note", ""),
        )
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect("moderation:queue")


@require_moderator
@require_http_methods(["POST"])
def suspension_draft(request):
    """Two-actor gate, step 1: a moderator writes the suspension down."""
    try:
        target_user = _load_or_raise(request.POST.get("user"))
        services.draft_suspension(
            moderator=request.user,
            target_user=target_user,
            reason=request.POST.get("reason", ""),
        )
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect("moderation:queue")


@require_moderator
@require_http_methods(["POST"])
def suspension_confirm(request, pk: int):
    """Two-actor gate, step 2: a different moderator confirms → account
    deactivated. The same-moderator refusal lives in the service (always on)."""
    try:
        draft = get_object_or_404(SuspensionDraft, pk=pk)
        services.confirm_suspension(draft, confirmer=request.user)
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect("moderation:queue")


@login_required
@require_http_methods(["POST"])
def block(request):
    """Member tool: stop seeing a member's content (reversible)."""
    try:
        target_user = _load_or_raise(request.POST.get("user"))
        services.block_user(blocker=request.user, blocked=target_user)
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect(_safe_next(request))


@login_required
@require_http_methods(["POST"])
def unblock(request):
    try:
        target_user = _load_or_raise(request.POST.get("user"))
        services.unblock_user(blocker=request.user, blocked=target_user)
    except ValidationError as exc:
        return _bad_request(exc)
    return redirect(_safe_next(request))


def _load_or_raise(raw_pk):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        return User.objects.get(pk=int(raw_pk))
    except (TypeError, ValueError, User.DoesNotExist):
        raise ValidationError(_("That member could not be found."))
