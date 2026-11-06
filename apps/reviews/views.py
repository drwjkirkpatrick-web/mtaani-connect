"""Reviews views (Prompt 22) — reads on the member's own surface, writes
through services. NO view publishes: even a successful POST leaves the row
HELD (DRAFT); publication happens ONLY in services.publish_due_reviews (the
sweep), which no route reaches. Deal refs resolve through each origin's
public read door (never imports), so REMOVED/unknown deals 404 honestly.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.types import ContentStatus, ReportReason
from apps.moderation.services import RateLimitError, report_content

from .forms import LeaveReviewForm
from .models import DealReview
from . import services

DRAFT = ContentStatus.DRAFT.value
PUBLISHED = ContentStatus.PUBLISHED.value


def _safe_next(request) -> str | None:
    """A local redirect target from POST (no open redirects, no //host)."""
    candidate = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return None


def _labelled(reviews) -> list[dict]:
    """Rows + honest deal titles + a per-row star row — template-safe plain
    dicts (never model internals across apps); the label fallback 'Deal
    #<id>' and the row shape live here so pages can't drift."""
    entries = []
    for review in reviews:
        entries.append(
            {
                "review": review,
                "deal_label": services.deal_label(review.deal_app, review.deal_id),
                "stars_row": [review.stars >= slot for slot in range(1, 6)],
            }
        )
    return entries


@login_required
def received_reviews(request):
    """GET /reviews/ — reviews I have RECEIVED (published only: held rows
    belong to the other party's own copy first — no preview leak), plus my
    reputation panel (the rollup Prompt 25/profile displays include)."""
    rows = DealReview.objects.filter(
        reviewed=request.user, status=PUBLISHED
    ).order_by("-created_at")
    return render(
        request,
        "reviews/received_list.html",
        {
            "entries": _labelled(rows),
            "reputation": services.reputation_for(request.user),
        },
    )


@login_required
def my_reviews(request):
    """GET /reviews/mine/ — reviews I have LEFT, all states, honestly
    labelled: a DRAFT row shows its hold reason (waiting for the counterpart
    or its 14-day unilateral release). Transparency, not secret moderation."""
    rows = DealReview.objects.filter(reviewer=request.user).order_by("-created_at")
    return render(request, "reviews/my_reviews.html", {"entries": _labelled(rows)})


@login_required
def leave_review(request):
    """GET shows the form for ?deal_app=&deal_id= (ref probed honestly);
    POST creates a HELD review. Submitting tells the member the publication
    rule (both-in-same-tick via the sweep, else 14 days) — the non-retaliation
    promise stated at the moment it matters."""
    deal_app = request.GET.get("deal_app", "")
    deal_id = request.GET.get("deal_id", "")
    initial = {}
    if deal_app and deal_id.isdigit():
        initial = {"deal_app": deal_app, "deal_id": int(deal_id)}
    form = LeaveReviewForm(initial=initial or None)
    deal_readable, deal_title = True, None

    if request.method == "POST":
        form = LeaveReviewForm(request.POST)
        if form.is_valid():
            try:
                review = services.create_deal_review(
                    reviewer=request.user,
                    deal_app=form.cleaned_data["deal_app"],
                    deal_id=form.cleaned_data["deal_id"],
                    stars=form.cleaned_data["stars"],
                    text=form.cleaned_data["text"],
                    dispute_note=form.cleaned_data["dispute_note"],
                )
            except ValidationError as exc:
                # Honest service refusals (already reviewed, stranger ref,
                # origin not landed, …) surface as form-level notes.
                form.add_error(None, " ".join(getattr(exc, "messages", None) or [str(exc)]))
            else:
                messages.success(
                    request,
                    _(
                        "Review saved. It publishes once the counterpart "
                        "reviews too, or automatically after 14 days."
                    ),
                )
                nxt = _safe_next(request)
                return redirect(nxt or ("reviews:my_reviews"))
    elif initial:
        # GET with a ref: pre-flight the deal parties so the form shows an
        # honest state (unknown/foreign deals explain themselves, no 500s).
        deal_readable, deal_notice = services.reviewable_deal(
            user=request.user, deal_app=deal_app, deal_id=int(deal_id)
        )
        if deal_readable:
            deal_title = services.deal_label(deal_app, int(deal_id))
        else:
            messages.error(request, deal_notice)
    return render(
        request,
        "reviews/review_form.html",
        {"form": form, "deal_readable": deal_readable, "deal_title": deal_title},
    )


def _target_review_or_404(review_id: int) -> DealReview:
    """A reportable review is a PUBLISHED one — held rows are private to the
    deal pair until the sweep speaks (reporting a secret nobody has seen)."""
    try:
        review = DealReview.objects.get(pk=review_id)
    except DealReview.DoesNotExist:
        raise Http404 from None
    if review.status != PUBLISHED:
        raise Http404
    return review


@login_required
@require_POST
def report_review(request, review_id):
    """POST /reviews/<id>/report/ — the abuse flow (Prompt 15's PUBLIC
    report_content; rate-limited 10/h). NOTE: moderation's target allow-list
    does not include 'reviews' yet (parent-owned file) — a not-yet-allowed
    target raises ValidationError, which we surface honestly instead of
    crashing; the queue sees review reports once the list is extended."""
    review = _target_review_or_404(review_id)
    reason = request.POST.get("reason", "")
    if reason not in ReportReason.values:
        messages.error(request, _("Pick a reason for the report."))
        return redirect("reviews:list")
    try:
        report_content(
            reporter=request.user,
            target=review,
            reason=reason,
            note=request.POST.get("note", ""),
        )
    except RateLimitError:
        messages.warning(request, _("Report limit reached — try again later."))
    except ValidationError as exc:
        # 'reviews' not in moderation's allow-list yet: honest refusal, no 500.
        messages.warning(
            request,
            " ".join(getattr(exc, "messages", None) or [str(exc)]),
        )
    else:
        messages.success(request, _("Thanks — the review is queued for moderators."))
    return redirect(_safe_next(request) or ("reviews:list"))
