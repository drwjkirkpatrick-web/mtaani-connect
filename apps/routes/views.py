"""Views for the route network (Prompt 25).

Public reads: index, search, route detail, stage detail — server-rendered.
Writes: corrections (login) + the moderator queue (staff) — human gate.
"""
from __future__ import annotations

import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.testing import assert_under_budget  # noqa: F401 (tests import path hint)

from . import gtfs as gtfs_seeder
from . import search as routes_search
from .forms import CorrectionForm
from .models import (
    CorrectionKind,
    CorrectionQueue,
    CorrectionStatus,
    Fare,
    Route,
    RouteStage,
    Stage,
)

PAGE_SIZE = 20

# Light anti-flood for the LOCAL queue (mirrors moderation.report_content's
# spirit — the rate limit there guards Report rows; 'routes' is not in
# moderation's ALLOWED_TARGET_APPS label list yet, see NOTE below).
MAX_CORRECTIONS_PER_HOUR = 10


def network_stats() -> dict[str, int]:
    """Honest counts for the index page (seed rows included, seed-flagged)."""
    return {
        "routes": Route.objects.count(),
        "stages": Stage.objects.count(),
        "saccos": Route.saccos.through.objects.values("sacco_id").distinct().count(),
        "fares": Fare.objects.count(),
    }


def index(request):
    """Route network entry: list + honest counts (spec: searchable network)."""
    routes = Route.objects.prefetch_related("saccos")[:PAGE_SIZE]
    context = {
        "routes": routes,
        "stats": network_stats(),
        "q": "",
        # HONEST state: whether a seed zip exists today (drives the manual-
        # entry hint; when False the network is whatever admins typed).
        "seeder_available": gtfs_seeder.available(),
    }
    return render(request, "routes/list.html", context)


def search(request):
    """?q= nickname / SACCO / destination search (trigram, alias-aware)."""
    q = request.GET.get("q", "")
    route_rows, stage_rows = routes_search.search_network(q)
    context = {
        "q": q,
        "route_rows": route_rows,
        "stage_rows": stage_rows,
        "saccos": [],  # grouped view: SACCO matches surface through routes
        "queried": bool(q.strip()),
    }
    return render(request, "routes/search.html", context)


def _line_info(route: Route) -> tuple[str, list | None]:
    """(kind, points) for the detail map — honesty lives here.

    WHY one helper: the pitfall rule is "never fabricate missing shapes"; the
    ONLY decision point about what may be drawn sits in code, not in a
    template's reach for defaults.
    """
    if route.shape_source == Route.ShapeSource.GTFS and route.shape_points:
        return "gtfs", route.shape_points
    stage_points = [
        (float(rs.stage.lon), float(rs.stage.lat))
        for rs in route.stagelinks.select_related("stage")
        if rs.stage.lat is not None and rs.stage.lon is not None
    ]
    if len(stage_points) >= 2:
        return "stage_to_stage", stage_points
    return "none", None


def route_detail(request, pk: int):
    """Ordered stages + observed fare RANGES + an honestly-labelled line."""
    route = get_object_or_404(
        Route, pk=pk
    )
    stages: list[RouteStage] = list(
        route.stagelinks.select_related("stage").order_by("position")
    )
    fares: list[Fare] = list(
        route.fares.select_related("from_stage", "to_stage")
        .order_by("from_stage__name", "to_stage__name")
    )
    line_kind, line_points = _line_info(route)
    context = {
        "route": route,
        "stages": stages,
        "fares": fares,
        "line_kind": line_kind,
        "line_points_json": json.dumps(line_points) if line_points else "null",
        "seed_quality": route.seed_origin,
        # WHERE the route without GTFS coords still shows SOMETHING: the
        # stage-to-stage join is rendered with its own explicit label.
    }
    return render(request, "routes/route_detail.html", context)


def stage_detail(request, pk: int):
    stage = get_object_or_404(Stage.objects.prefetch_related("aliases"), pk=pk)
    routes = list(
        RouteStage.objects.filter(stage=stage)
        .select_related("route")
        .order_by("route__name_en", "position")
    )
    fares = list(
        Fare.objects.filter(from_stage=stage)
        .exclude(to_stage=stage)
        .select_related("route", "from_stage", "to_stage")[:PAGE_SIZE]
    )
    context = {"stage": stage, "routes": routes, "fares": fares}
    return render(request, "routes/stage_detail.html", context)


# NOTE: corrections live in a LOCAL CorrectionQueue instead of moderation.
# Report rows today — moderation's ALLOWED_TARGET_APPS does not include
# 'routes' yet; the parent wires the label at consolidation (see tests
# test_correction_queue_flow + the NOTE in apps/routes/gtfs.py data README).
# The row layout intentionally mirrors moderation.Report so a later copy
# across is mechanical.


@login_required
def correction_new(request):
    """Open a correction (wrong stage position / missing route / fare)."""
    if request.method == "POST":
        window_start = timezone.now() - timedelta(hours=1)
        recent = CorrectionQueue.objects.filter(
            opened_by=request.user, created_at__gte=window_start
        ).count()
        if recent >= MAX_CORRECTIONS_PER_HOUR:
            messages.error(
                request,
                _("You have reached the hourly limit of %(limit)s corrections.") % {
                    "limit": MAX_CORRECTIONS_PER_HOUR,
                },
            )
            return redirect("routes:corrections_mine")
        form = CorrectionForm(request.POST)
        if form.is_valid():
            try:
                item = form.save(commit=False)
                item.opened_by = request.user
                item.full_clean(exclude=["opened_by"])
                item.save()
                # WHY no auto-action: corrections DRAFT evidence for a human
                # moderator — nothing edits the network unattended.
                messages.success(request, _("Thanks — your correction is queued for review."))
                return redirect("routes:corrections_mine")
            except ValidationError as exc:
                form.add_error(None, exc.messages)
    else:
        initial = {}
        route_id = request.GET.get("route")
        stage_id = request.GET.get("stage")
        if route_id:
            initial["route"] = route_id
        if stage_id:
            initial["stage"] = stage_id
        form = CorrectionForm(initial=initial)
    return render(
        request,
        "routes/correction_form.html",
        {"form": form, "kinds": CorrectionKind.choices},
    )


@login_required
def corrections_mine(request):
    items = CorrectionQueue.objects.filter(opened_by=request.user)[:PAGE_SIZE]
    return render(request, "routes/corrections_mine.html", {"items": items})


@login_required
@require_POST
def correction_resolve(request, pk: int):
    """Moderator resolves a correction (staff-only POST — the human gate)."""
    if not request.user.is_staff:
        raise PermissionDenied(_("Only moderators resolve corrections."))
    item = get_object_or_404(CorrectionQueue, pk=pk)
    item.resolution_note = (request.POST.get("resolution_note") or "").strip()
    item.status = CorrectionStatus.RESOLVED.value
    item.save(update_fields=["resolution_note", "status", "updated_at"])
    messages.success(request, _("Correction resolved."))
    return redirect("routes:queue")


@login_required
def correction_queue(request):
    """Staff-only queue: open corrections first (deterministic order)."""
    if not request.user.is_staff:
        raise PermissionDenied(_("Only moderators see the queue."))
    items = CorrectionQueue.objects.select_related(
        "opened_by", "route", "stage"
    ).filter(status=CorrectionStatus.OPEN.value)[:PAGE_SIZE]
    resolved = CorrectionQueue.objects.filter(
        status=CorrectionStatus.RESOLVED.value
    )[:PAGE_SIZE]
    return render(
        request,
        "routes/queue.html",
        {"items": items, "resolved": resolved},
    )
