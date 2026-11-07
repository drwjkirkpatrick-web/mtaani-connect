"""Views for the alerts app (Prompt 31).

ALL reads are about the RIDER'S OWN follows (login-gated; rows are filtered
by request.user — a forged URL can never touch someone else's follow).
Writes are POST-only one-tap actions; every message says what happened
(and consent changes say on/off, never silent). The arrivals page + its
JSON mirror share services.stage_arrivals (one truth, no drift).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.routes.models import Route, Stage

from . import services
from .models import RouteFollow, VehicleFollow

PAGE_SIZE = 20  # bounded lists (hard rule): follows grow to 42-prompt scale


def _follow_or_404(kind: str, pk: int, user):
    """The rider's own follow row of the given kind (404 otherwise)."""
    if kind == "vehicle":
        return get_object_or_404(VehicleFollow, pk=pk, user=user)
    if kind == "route":
        return get_object_or_404(RouteFollow, pk=pk, user=user)
    raise Http404


def _first_validation_message(exc: ValidationError) -> str:
    """One human message from a ValidationError (dict or single) — full_clean
    raises a DICT, so views must not assume ``exc.message`` exists."""
    if getattr(exc, "error_dict", None):
        key = "__all__" if "__all__" in exc.error_dict else next(iter(exc.error_dict))
        return str(exc.error_dict[key][0])
    return str(getattr(exc, "message", exc))


def index(request):
    """/alerts/ → the member's follows (the CRUD + toggles live there)."""
    return redirect(reverse("alerts:follows"))


# ----------------------------------------------------------------- helpers
def _stage_options_for(follow) -> list[tuple[int, str]]:
    """(stage_pk, name) pairs the follow MAY pick: on the followed path,
    deduped (looping routes can revisit a stage), name-ordered."""
    if isinstance(follow, RouteFollow):
        rows = follow.route.stage_order.values_list(
            "stage_id", "stage__name"
        )
    elif isinstance(follow, VehicleFollow):
        rows = (
            Stage.objects.filter(
                routelinks__route__in=follow.vehicle.routes.all()
            )
            .values_list("pk", "name")
            .order_by("name")
        )
    else:
        return []
    seen: set[int] = set()
    options = []
    for pk, name in rows:
        if pk not in seen:
            seen.add(pk)
            options.append((pk, name))
    return options


def _arrivals_coords(follow) -> tuple[int, int] | None:
    """(route_id, stage_id) for the arrivals link when a stage is chosen
    AND it lies on a route the follow targets; None otherwise."""
    stage = follow.notify_stage
    if stage is None:
        return None
    if isinstance(follow, RouteFollow):
        return (follow.route_id, stage.pk)
    link = (
        stage.routelinks.filter(route__in=follow.vehicle.routes.all())
        .order_by("route_id")
        .first()
    )
    return (link.route_id, stage.pk) if link else None


# ------------------------------------------------------------------- views
@login_required
def my_follows(request):
    """The CRUD surface: add (nickname/route), toggle, stage, radius list."""
    vehicle_items = []
    for follow in (
        VehicleFollow.objects.filter(user=request.user)
        .select_related("vehicle", "notify_stage")[:PAGE_SIZE]
    ):
        coords = _arrivals_coords(follow)
        vehicle_items.append(
            {
                "row": follow,
                "stage_options": _stage_options_for(follow),
                "arrivals_url": (
                    reverse(
                        "alerts:arrivals",
                        kwargs={"route_id": coords[0], "stage_id": coords[1]},
                    )
                    if coords
                    else None
                ),
            }
        )
    route_items = []
    for follow in (
        RouteFollow.objects.filter(user=request.user)
        .select_related("route", "notify_stage")[:PAGE_SIZE]
    ):
        coords = _arrivals_coords(follow)
        route_items.append(
            {
                "row": follow,
                "stage_options": _stage_options_for(follow),
                "arrivals_url": (
                    reverse(
                        "alerts:arrivals",
                        kwargs={"route_id": coords[0], "stage_id": coords[1]},
                    )
                    if coords
                    else None
                ),
            }
        )
    return render(
        request,
        "alerts/my_follows.html",
        {"vehicle_items": vehicle_items, "route_items": route_items},
    )


@login_required
@require_POST
def follow_vehicle(request):
    """Add a matatu follow by NICKNAME (resolved to a unique ACTIVE row)."""
    try:
        follow = services.follow_vehicle(user=request.user, nickname=request.POST.get("nickname", ""))
        messages.success(
            request,
            services.MSG_FOLLOWED
            % {"name": services.vehicle_display(follow.vehicle)},
        )
    except (services.AmbiguousNickname, services.NicknameNotFound) as exc:
        messages.error(request, str(exc))
    except ValidationError as exc:
        messages.error(request, _first_validation_message(exc))
    return redirect("alerts:follows")


@login_required
@require_POST
def follow_route(request):
    """Add a route follow (route must exist; duplicate refused)."""
    pk = request.POST.get("route_id")
    if not (pk and str(pk).isdigit()):
        messages.error(request, _("Pick a route to follow."))
        return redirect("alerts:follows")
    route = get_object_or_404(Route, pk=int(pk))
    try:
        follow = services.follow_route(user=request.user, route=route)
        messages.success(
            request, services.MSG_FOLLOWED % {"name": route.name_en}
        )
    except ValidationError as exc:
        messages.error(request, _first_validation_message(exc))
    return redirect("alerts:follows")


@login_required
@require_POST
def remove_follow(request, kind: str, pk: int):
    """Unfollow in one tap (idempotent below the service)."""
    follow = _follow_or_404(kind, pk, request.user)
    if kind == "vehicle":
        removed = services.unfollow_vehicle(user=request.user, vehicle=follow.vehicle)
        name = services.vehicle_display(follow.vehicle)
    else:
        removed = services.unfollow_route(user=request.user, route=follow.route)
        name = follow.route.name_en
    if removed:
        messages.success(request, services.MSG_FOLLOW_REMOVED % {"name": name})
    return redirect("alerts:follows")


@login_required
@require_POST
def toggle_notify(request, kind: str, pk: int):
    """The ONE-TAP notify switch — consent stated back in words."""
    follow = _follow_or_404(kind, pk, request.user)
    name = (
        services.vehicle_display(follow.vehicle)
        if kind == "vehicle"
        else follow.route.name_en
    )
    enabled = services.toggle_notify(follow)
    messages.success(
        request,
        (services.MSG_NOTIFIED_ON if enabled else services.MSG_NOTIFIED_OFF)
        % {"name": name},
    )
    return redirect("alerts:follows")


@login_required
@require_POST
def set_stage(request, kind: str, pk: int):
    """Pick the alert stage from the follow's own path (empty = clear)."""
    follow = _follow_or_404(kind, pk, request.user)
    raw = (request.POST.get("stage_id") or "").strip()
    if not raw:
        services.set_alert_stage(follow, None)
        messages.success(request, _("Alert stage cleared — no approach alerts until you pick one."))
        return redirect("alerts:follows")
    stage = get_object_or_404(Stage, pk=int(raw))
    try:
        services.set_alert_stage(follow, stage)
        messages.success(request, services.MSG_STAGE_SET % {"stage": stage.name})
    except ValidationError as exc:
        messages.error(request, _first_validation_message(exc))
    return redirect("alerts:follows")


@login_required
@require_POST
def set_radius(request, kind: str, pk: int):
    """Set the approach radius (1–10 km; bad input is a message, not a 500)."""
    follow = _follow_or_404(kind, pk, request.user)
    raw = (request.POST.get("radius_km") or "").strip()
    try:
        km = int(raw)
    except ValueError:
        km = -1
    try:
        services.set_radius(follow, km)
        messages.success(request, services.MSG_RADIUS_SET % {"km": km})
    except ValidationError as exc:
        messages.error(request, _first_validation_message(exc))
    return redirect("alerts:follows")


# ------------------------------------------------------------- arrivals
def _arrivals_payload(route_id: int, stage_id: int, user, now):
    route = get_object_or_404(Route, pk=route_id)
    stage = get_object_or_404(Stage, pk=stage_id)
    data = services.stage_arrivals(route, stage, user=user, now=now)
    return route, stage, data


@login_required
def stage_arrivals_page(request, route_id: int, stage_id: int):
    """THE acceptance surface: 'my matatu is coming' — or an honest line."""
    route, stage, data = _arrivals_payload(
        route_id, stage_id, request.user, timezone.now()
    )
    return render(
        request,
        "alerts/arrivals.html",
        {"route": route, "stage": stage, "rows": data["rows"]},
    )


@login_required
def stage_arrivals_json(request, route_id: int, stage_id: int):
    """The JSON mirror of the page — same rows, same labels, machine-read."""
    route, stage, data = _arrivals_payload(
        route_id, stage_id, request.user, timezone.now()
    )
    rows = []
    for row in data["rows"]:
        clean = {
            k: (str(v) if v is not None and not isinstance(v, (dict, bool, int, float)) else v)
            for k, v in row.items()
        }
        rows.append(clean)
    return JsonResponse(
        {
            "route_id": route.pk,
            "stage_id": stage.pk,
            "stage": stage.name,
            "rows": rows,
            "generated_at": timezone.now().isoformat(),
        }
    )
