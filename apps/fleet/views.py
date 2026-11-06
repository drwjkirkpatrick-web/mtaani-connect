"""Operator dashboard views (Prompt 26).

TENANT RULE: no view touches Vehicle/CrewMember without going through
services.visible_vehicles(user) / pending_crew_qs / crew_seat_for — the
queryset itself excludes other SACCOs' rows, so a forged URL or POST gets
403 (PermissionDenied), never the other SACCO's data. Templates receive
tenant-scoped querysets only; nothing is checked "in the template".

Public read (no operator gate): the fleet search page — plates, nicknames
and route names are business data; no personal contact anywhere (KDPA).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import services
from .forms import CrewRequestForm, VehicleForm
from .models import CrewStatus


# ---------------------------------------------------------------- helpers
def _tenant_vehicle(request, pk: int):
    """The vehicle IFF it belongs to one of the requester's SACCOs."""
    vehicle = services.visible_vehicles(request.user).filter(pk=pk).first()
    if vehicle is None:
        raise PermissionDenied
    return vehicle


def _tenant_seat(request, seat_pk: int):
    """Crew seat IFF its vehicle is inside the requester's tenancy."""
    seat = services.crew_seat_for(request.user, seat_pk)
    if seat is None:
        raise PermissionDenied
    return seat


def _require_operator(request):
    if not services.saccos_of_admin(request.user).exists():
        raise PermissionDenied


def _flash_error(redirect_to, exc: ValidationError):
    messages.error(redirect_to, "; ".join(exc.messages))


# ------------------------------------------------------------- operator
@login_required
def dashboard(request):
    """SACCO admin's operator dashboard — THEIR vehicles only (query layer)."""
    _require_operator(request)
    return render(request, "fleet/dashboard.html", services.operator_dashboard_context(request.user))


@login_required
def vehicle_register(request):
    """New vehicle: picks a sacco the requester administers (form-enforced)."""
    _require_operator(request)
    form = VehicleForm(user=request.user)
    if request.method == "POST":
        form = VehicleForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                services.register_vehicle(
                    sacco=form.cleaned_data["sacco"],
                    raw_plate=form.cleaned_data["plate"],
                    nickname=form.cleaned_data["nickname"],
                    capacity=form.cleaned_data["capacity"],
                    routes=list(form.cleaned_data["routes"]),
                    actor=request.user,
                )
            except ValidationError as exc:
                _flash_error(request, exc)
            else:
                messages.success(request, _("Vehicle registered."))
                return redirect("fleet:dashboard")
    return render(request, "fleet/vehicle_form.html", {"form": form})


@login_required
def vehicle_detail(request, pk: int):
    vehicle = _tenant_vehicle(request, pk)
    from apps.routes.models import Route  # shared network data (public read)

    return render(
        request,
        "fleet/vehicle_detail.html",
        {
            "vehicle": vehicle,
            "crew": vehicle.crew.select_related("user").order_by("-created_at"),
            "route_options": Route.objects.order_by("name_en"),
            "vehicle_route_ids": set(vehicle.routes.values_list("pk", flat=True)),
        },
    )


@login_required
@require_POST
def vehicle_routes(request, pk: int):
    """Assign operating routes to one of OUR vehicles."""
    vehicle = _tenant_vehicle(request, pk)
    services.update_vehicle_routes(
        vehicle, route_pks=request.POST.getlist("routes"), actor=request.user
    )
    messages.success(request, _("Routes saved."))
    return redirect("fleet:vehicle_detail", pk=vehicle.pk)


@login_required
@require_POST
def vehicle_retire(request, pk: int):
    """Pull a vehicle out of service — future approvals stop at this door."""
    vehicle = _tenant_vehicle(request, pk)
    try:
        services.retire_vehicle(vehicle, actor=request.user)
    except ValidationError as exc:
        _flash_error(request, exc)
    else:
        messages.warning(request, _("Vehicle retired."))
    return redirect("fleet:dashboard")


# ------------------------------------------------------------------ crew
@login_required
def crew_request(request):
    """A member self-requests a seat (row lands PENDING — no authority)."""
    form = CrewRequestForm()
    if request.method == "POST":
        form = CrewRequestForm(request.POST)
        if form.is_valid():
            try:
                seat = services.request_crew(
                    user=request.user,
                    vehicle=form.cleaned_data["vehicle"],
                    role=form.cleaned_data["role"],
                )
            except ValidationError as exc:
                _flash_error(request, exc)
            else:
                messages.success(request, _("Crew seat requested — waiting for the SACCO admin."))
                return redirect("fleet:crew_mine")
    return render(request, "fleet/crew_form.html", {"form": form})


@login_required
def crew_mine(request):
    """The requester's own seats + states (transparency, KDPA-friendly)."""
    seats = (
        request.user.fleet_crew.select_related("vehicle__sacco")
        if request.user.is_authenticated
        else []
    )
    return render(request, "fleet/crew_mine.html", {"seats": seats})


@login_required
def crew_pending(request):
    """Approval queue: PENDING seats on OUR vehicles only."""
    _require_operator(request)
    seats = services.pending_crew_qs(request.user)
    return render(request, "fleet/crew_pending.html", {"seats": seats})


@login_required
@require_POST
def crew_approve(request, seat_pk: int):
    """SAY YES: same-sacco admin and NOT the applicant (two-party gate)."""
    seat = _tenant_seat(request, seat_pk)
    try:
        services.approve_crew(seat, approver=request.user)
    except ValidationError as exc:
        _flash_error(request, exc)
    else:
        messages.success(request, _("Crew seat approved."))
    return redirect("fleet:crew_pending")


@login_required
@require_POST
def crew_revoke(request, seat_pk: int):
    """APPROVED → REVOKED; the trail keeps who said no, when."""
    seat = _tenant_seat(request, seat_pk)
    try:
        services.revoke_crew(seat, revoker=request.user)
    except ValidationError as exc:
        _flash_error(request, exc)
    else:
        messages.warning(request, _("Crew seat revoked."))
    return redirect("fleet:crew_roster", vehicle_pk=seat.vehicle.pk)


@login_required
def crew_roster(request, vehicle_pk: int):
    """A crew board for one of OUR vehicles: all seats, all states."""
    vehicle = _tenant_vehicle(request, vehicle_pk)
    seats = vehicle.crew.select_related("user").order_by("role", "status")
    return render(
        request,
        "fleet/crew_roster.html",
        {"vehicle": vehicle, "seats": seats, "approved_count": seats.filter(status=CrewStatus.APPROVED.value).count()},
    )


# ----------------------------------------------------------------- public
def search(request):
    """?q= plate-prefix / nickname / route-name search over ACTIVE vehicles."""
    q = (request.GET.get("q") or "")
    vehicles = services.search_vehicles(q)
    return render(
        request,
        "fleet/search.html",
        {"q": q, "vehicles": vehicles, "queried": bool(q.strip())},
    )
