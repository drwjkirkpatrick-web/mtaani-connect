"""Disputes views (Prompt 23) — member surfaces + the moderator queue.

Every write goes through services; no view touches status columns. The
moderator tools are role-gated by moderation's PUBLIC is_moderator (403 for
everyone else, same as the moderation queue). Action refusals from services
land on the page as plain localized messages — the human gate always
explains itself."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.moderation.services import is_moderator

from . import services
from .forms import OpenCaseForm, ProposalForm
from .models import DisputeCase, DisputeProposal, FraudFlag


def _case_or_404(case_id: int) -> DisputeCase:
    return get_object_or_404(DisputeCase, pk=case_id)


def _detail_or_denied(request, case_id: int):
    """Resolve the case + the access gate (opener / deal party / moderator);
    a stranger sees the SAME 404 as a missing case (IDOR shape)."""
    case = _case_or_404(case_id)
    if not services.can_access_case(case, request.user):
        raise Http404
    return case


def _exc_messages(exc) -> str:
    return " ".join(getattr(exc, "messages", None) or [str(exc)])


@login_required
def open_case(request):
    """GET/POST /disputes/open/?deal_app=&deal_id= — a party opens THE case
    on their deal. Evidence auto-links from the negotiation trail; the
    pre-flight below is advisory only (the service owns the real gate)."""
    deal_app = request.GET.get("deal_app") or request.POST.get("deal_app", "")
    deal_id = request.POST.get("deal_id") or request.GET.get("deal_id", "")

    deal_label = None
    if deal_app and str(deal_id).isdigit():
        try:
            if deal_app == "barter":
                from apps.barter import services as barter_services

                deal_label = barter_services.deal_label(int(deal_id))
            elif deal_app == "listings":
                from apps.listings.services import get_visible_listing

                row = get_visible_listing(int(deal_id), request.user)
                deal_label = str(row.title) if row else None
            elif deal_app == "services":
                from apps.services.services import get_visible_service

                row = get_visible_service(int(deal_id), request.user)
                deal_label = str(row.title) if row else None
        except Exception:  # noqa: BLE001 — a label is never load-bearing
            pass

    if request.method == "POST":
        form = OpenCaseForm(request.POST)
        if form.is_valid():
            try:
                case = services.open_case(
                    opener=request.user,
                    deal_app=form.cleaned_data["deal_app"],
                    deal_id=form.cleaned_data["deal_id"],
                    what_promised=form.cleaned_data["what_promised"],
                    what_happened=form.cleaned_data["what_happened"],
                )
            except ValidationError as exc:
                form.add_error(None, _exc_messages(exc))
            else:
                messages.success(
                    request,
                    _(
                        "Case opened. Negotiation evidence is attached; a "
                        "moderator will review it."
                    ),
                )
                return redirect("disputes:detail", case_id=case.pk)
    else:
        form = OpenCaseForm(
            initial={"deal_app": deal_app, "deal_id": deal_id}
            if deal_app and str(deal_id).isdigit()
            else None
        )
    return render(
        request,
        "disputes/case_form.html",
        {"form": form, "deal_label": deal_label},
    )


@login_required
def my_cases(request):
    """GET /disputes/mine/ — cases I opened or that are open against me,
    each with its honest aging tier."""
    cases = [
        (case, services.aging_tier(case), services.case_label(case))
        for case in services.cases_for(request.user)
    ]
    return render(request, "disputes/my_cases.html", {"cases": cases})


@login_required
def queue(request):
    """GET /disputes/queue/ — the admin dashboard: the case queue with aging
    tiers (escalated first, spec verbatim), pending proposals, and the
    recent fraud flags — ANNOTATIONS awaiting a human, shown next to the
    cases they may produce."""
    if not is_moderator(request.user):
        raise PermissionDenied
    escalated_ids = {case.pk for case in services.escalated_cases()}
    rows = []
    for case in services.queue_cases():
        rows.append(
            {
                "case": case,
                "tier": services.aging_tier(case),
                "label": services.case_label(case),
                "escalated": case.pk in escalated_ids,
            }
        )
    rows.sort(key=lambda item: 0 if item["escalated"] else 1)
    proposals = [
        {"proposal": proposal, "label": services.case_label(proposal.case)}
        for proposal in DisputeProposal.objects.filter(confirmed_at__isnull=True)
        .select_related("case", "proposed_by")[:20]
    ]
    recent_flags = list(FraudFlag.objects.order_by("-created_at")[:20])
    return render(
        request,
        "disputes/queue.html",
        {"rows": rows, "proposals": proposals, "recent_flags": recent_flags},
    )


@login_required
def detail(request, case_id: int):
    """GET /disputes/<id>/ — claim + evidence (lazily resolved, gated) +
    proposals + the moderator actions. Evidence keeps its ids after a case
    resolves: evidence retention is the point."""
    case = _detail_or_denied(request, case_id)
    return render(
        request,
        "disputes/case_detail.html",
        {
            "case": case,
            "label": services.case_label(case),
            "tier": services.aging_tier(case),
            "evidence": services.evidence_rows_for(case, request.user),
            "proposals": case.proposals.select_related(
                "proposed_by", "confirmed_by", "target_ct"
            ),
            "moderator": is_moderator(request.user),
            "proposal_form": ProposalForm(),
            "is_opener": case.opener_id == request.user.pk,
        },
    )


@login_required
@require_POST
def propose(request, case_id: int):
    """POST /disputes/<id>/propose/ — gate step 1 (executes nothing)."""
    if not is_moderator(request.user):
        raise PermissionDenied
    case = _case_or_404(case_id)
    form = ProposalForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick an outcome to propose."))
        return redirect("disputes:detail", case_id=case.pk)
    try:
        services.propose_outcome(
            case,
            proposer=request.user,
            action=form.cleaned_data["action"],
            note=form.cleaned_data["note"],
        )
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(
            request,
            _(
                "Outcome proposed. A different moderator must confirm it "
                "before anything happens."
            ),
        )
    return redirect("disputes:detail", case_id=case.pk)


@login_required
@require_POST
def confirm(request, case_id: int):
    """POST /disputes/<id>/confirm/<proposal_id>/ — gate step 2: execute."""
    if not is_moderator(request.user):
        raise PermissionDenied
    case = _case_or_404(case_id)
    proposal = get_object_or_404(DisputeProposal, pk=request.POST.get("proposal"))
    if proposal.case_id != case.pk:
        raise Http404
    try:
        services.confirm_proposal(
            proposal,
            confirmer=request.user,
            decision_note=request.POST.get("decision_note", ""),
        )
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(request, _("Confirmed — the outcome is executed."))
    return redirect("disputes:detail", case_id=case.pk)


@login_required
@require_POST
def start_review(request, case_id: int):
    """POST /disputes/<id>/start-review/ — a moderator picks the case up."""
    if not is_moderator(request.user):
        raise PermissionDenied
    case = _case_or_404(case_id)
    try:
        services.start_review(case, moderator=request.user)
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(request, _("Case under review."))
    return redirect("disputes:detail", case_id=case.pk)


@login_required
@require_POST
def evidence_add(request, case_id: int):
    """POST /disputes/<id>/evidence/ — kind=text|image. text: a note;
    image: a MediaRef id (your own; moderators may gather any)."""
    case = _detail_or_denied(request, case_id)
    kind = request.POST.get("kind", "text")
    try:
        if kind == "image":
            services.attach_image_evidence(
                case=case,
                added_by=request.user,
                image=request.POST.get("image_ref", ""),
            )
        else:
            services.attach_text_evidence(
                case=case,
                added_by=request.user,
                text=request.POST.get("text", ""),
            )
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(request, _("Evidence attached."))
    return redirect("disputes:detail", case_id=case.pk)


@login_required
@require_POST
def resolve_route(request, case_id: int):
    """POST /disputes/<id>/resolve/ — close as handled (human only)."""
    if not is_moderator(request.user):
        raise PermissionDenied
    case = _case_or_404(case_id)
    try:
        services.resolve_case(
            case,
            moderator=request.user,
            decision_note=request.POST.get("decision_note", ""),
        )
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(request, _("Case resolved."))
    return redirect("disputes:detail", case_id=case.pk)


@login_required
@require_POST
def dismiss(request, case_id: int):
    """POST /disputes/<id>/dismiss/ — close as dismissed (human only)."""
    if not is_moderator(request.user):
        raise PermissionDenied
    case = _case_or_404(case_id)
    try:
        services.dismiss_case(
            case,
            moderator=request.user,
            decision_note=request.POST.get("decision_note", ""),
        )
    except ValidationError as exc:
        messages.error(request, _exc_messages(exc))
    else:
        messages.success(request, _("Case dismissed."))
    return redirect("disputes:detail", case_id=case.pk)


@login_required
def queue_redirect(request):
    """GET /disputes/ lands members on THEIR case list; moderators are sent
    to the dashboard queue."""
    if is_moderator(request.user):
        return redirect("disputes:queue")
    return redirect("disputes:my_cases")
