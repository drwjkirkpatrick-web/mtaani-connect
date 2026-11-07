"""views.py — Prompt 40's KDPA surfaces (app_name='compliance').

Roles (scoping is tested in tests/test_compliance.py):
* compliance:dashboard — STAFF: the DataMap table (what/why/basis/retention
  per row) + the DSAR queue — the auditor page. The registry is upserted
  from the hand-maintained inventory on every visit, so the page can
  never show a stale map.
* compliance:my_data — MEMBER: export button (self-serve access), the
  deletion-request door, the correction pointer (profiles:me — self-serve
  correction per the DPA) and their OWN consent timeline.
* compliance:requests + compliance:request_detail — STAFF: the queue and
  the fulfil/refuse actions; fulfilling DELETE runs anonymize_account; an
  ACCESS fulfilment hands the generated archive.
* compliance:user_consents — STAFF: any member's consent timeline
  (searched by username — evidence view, editable nothing).
* compliance:consent_export — STAFF: the ledger as CSV-ish text for the
  ODPC demonstration.
"""
from __future__ import annotations

import csv
import io

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.compliance import services
from apps.compliance.models import DataRequest, RequestKind, RequestStatus


# --------------------------------------------------------------- member side
@login_required
def my_data(request):
    """The member's KDPA page: export, deletion request, consents."""
    user = request.user
    if request.method == "POST":
        kind = request.POST.get("kind")
        note = (request.POST.get("note") or "").strip()
        try:
            services.open_data_request(user, kind=kind or "", note=note)
        except services.AlreadyAnonymized as exc:  # unreachable here, kept honest
            messages.error(request, str(exc))
        except Exception as exc:  # noqa: BLE001 — one message line, why included
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Request recorded in the DSAR ledger."))
        return redirect("compliance:my_data")

    context = {
        "my_requests": user.data_requests.all().order_by("-created_at"),
        "consents": user.consent_events.all().order_by("-created_at"),
        "kinds": RequestKind,
        "open_delete": user.data_requests.filter(
            status=RequestStatus.OPEN.value, kind=RequestKind.DELETE.value,
        ).exists(),
    }
    return render(request, "compliance/my_data.html", context)


@login_required
def export_my_data(request):
    """Self-serve access: the readable archive, downloaded."""
    body = services.export_archive(request.user, now=timezone.now())
    response = HttpResponse(body, content_type="text/plain; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="mtaani-my-data-{request.user.pk}.txt"'
    )
    return response


# ----------------------------------------------------------------- staff side
@staff_member_required
def dashboard(request):
    """Auditor page: the whole DataMap + the DSAR queue."""
    services.load_inventory()  # registry moves with code — always current
    from apps.compliance.models import DataMapEntry

    entries = DataMapEntry.objects.order_by("app_label", "model_name")
    open_qs = DataRequest.objects.filter(status=RequestStatus.OPEN.value)
    recent_qs = DataRequest.objects.exclude(status=RequestStatus.OPEN.value)[:20]
    return render(request, "compliance/dashboard.html", {
        "entries": entries,
        "open_requests": open_qs,
        "recent_requests": recent_qs,
    })


@staff_member_required
def requests_view(request):
    """The DSAR queue, with the member searchable by username."""
    q = (request.GET.get("q") or "").strip()
    qs = DataRequest.objects.select_related("user", "handled_by")
    if q:
        qs = qs.filter(user__username__icontains=q)
    return render(request, "compliance/requests.html", {
        "requests": qs[:200],
        "q": q,
        "kinds": RequestKind,
        "statuses": RequestStatus,
    })


@staff_member_required
def request_detail(request, pk: int):
    """One DSAR: see context, fulfil or refuse with a stamped reason."""
    req = get_object_or_404(
        DataRequest.objects.select_related("user", "handled_by"), pk=pk,
    )
    archive_text = None
    if request.method == "POST":
        action = request.POST.get("action")
        note = (request.POST.get("note") or "").strip()
        try:
            if action == "fulfill":
                if req.kind == RequestKind.DELETE.value:
                    services.fulfill_deletion_request(req, by=request.user, note=note)
                elif req.kind == RequestKind.ACCESS.value:
                    archive_text = services.export_archive(req.user, now=timezone.now())
                    req = services.fulfill_request(
                        req, by=request.user, now=timezone.now(),
                        note=note or _("archive generated in-line for the member"),
                    )
                else:
                    req = services.fulfill_request(
                        req, by=request.user, now=timezone.now(),
                        note=note or _("self-serve correction at profiles:me"),
                    )
                messages.success(request, _("Request fulfilled."))
            elif action == "refuse":
                services.refuse_request(req, by=request.user, note=note)
                messages.success(request, _("Request refused with reason."))
            else:
                messages.error(request, _("Unknown action."))
        except Exception as exc:  # noqa: BLE001 — one honest message line
            messages.error(request, str(exc))
        return redirect("compliance:request_detail", pk=req.pk)

    if req.kind == RequestKind.ACCESS.value and req.status == RequestStatus.OPEN.value:
        archive_text = services.export_archive(req.user, now=timezone.now())
    return render(request, "compliance/request_detail.html", {
        "req": req,
        "archive_text": archive_text,
        "statuses": RequestStatus,
    })


@staff_member_required
def user_consents(request):
    """Any member's consent timeline (staff search door)."""
    username = (request.GET.get("user") or "").strip()
    User = get_user_model()
    member = User.objects.filter(username=username).first() if username else None
    return render(request, "compliance/user_consents.html", {
        "searched": username,
        "member": member,
        "consents": member.consent_events.all().order_by("-created_at") if member else [],
    })


@staff_member_required
def consent_export(request):
    """The consent ledger as CSV-ish text (ODPC demonstration evidence).

    Optional ?user=<username> filter exports ONE member's rows.
    """
    username = (request.GET.get("user") or "").strip()
    from apps.accounts.models import ConsentEvent

    rows = ConsentEvent.objects.select_related("user").order_by("created_at")
    if username:
        rows = rows.filter(user__username=username)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_pk", "username", "purpose_key", "doc_version",
                     "source_ip", "created_at"])
    for ce in rows:
        writer.writerow([ce.user_id, ce.user.username, ce.purpose_key,
                         ce.doc_version, ce.source_ip or "",
                         ce.created_at.strftime("%Y-%m-%d %H:%M")])
    stamp = timezone.now().strftime("%Y%m%d")
    response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="consent-ledger-{stamp}.csv"'
    )
    return response
