"""Crew + operator tracking views (Prompt 27).

RULES the views implement structurally:
  * login_required everywhere (the doors are humans with fleet approval);
  * the vehicle authorization gate comes from services.authorization_for —
    the SAME door start/end/rotate enforce; a forged URL gets 403
    (PermissionDenied), never another SACCO's session;
  * ALL position visibility happens through services.visible_positions —
    no view implements its own filter (the pitfall: these pages cannot
    leak positions that the query layer suppresses);
  * the raw token is rendered EXACTLY ONCE, by the create/rotate POST
    response itself (no redirect, no stored raw token, nothing to refetch
    — refresh shows the page without it);
  * NO public map here (28 owns it); no serializer leaks crew identity.

KDPA/DPIA: pages show authorization facts + derived status + privacy-
filtered position COUNTS — never coordinates, never crew identity.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from . import services
from .models import TrackingSession

# Cap input bounds shown to the user; the service re-validates (defense —
# the view's parse is presentation, the service's rule is the contract).
_CAP_MIN, _CAP_MAX = 1, services.DEFAULT_CAP_HOURS


def _my_session(request, pk: int) -> TrackingSession:
    """The session IFF its vehicle is one the requester may manage — the
    tenancy door is authorizable_vehicles (a union of already-scoped
    doors); None → 403, never another SACCO's row."""
    session = (
        TrackingSession.objects.filter(
            pk=pk, vehicle__in=services.authorizable_vehicles(request.user)
        )
        .select_related("vehicle__sacco")
        .first()
    )
    if session is None:
        raise PermissionDenied
    return session


def _parse_cap_hours(raw: str) -> int | None:
    """'12' → 12; ''/garbage → None (default). The SERVICE owns the rule;
    parsing only shapes the input for it (bad input becomes the default —
    the service would have refused an out-of-bounds value, but silently
    defaulting is the gentler, equally-capped path)."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def _flash_error(target, exc: ValidationError):
    messages.error(target, "; ".join(exc.messages))


# ----------------------------------------------------------------- pages
@login_required
def session_start(request):
    """Start a session: pick an authorized vehicle (+ optional shorter
    cap). POST renders the token page DIRECTLY — display-once."""
    rows = []
    now = timezone.now()
    for vehicle in services.authorizable_vehicles(request.user):
        live = services.live_session_for(vehicle, now=now)
        rows.append({"vehicle": vehicle, "live_session": live})
    raw_token = None
    if request.method == "POST":
        vehicle_id = request.POST.get("vehicle")
        vehicle = next(
            (r["vehicle"] for r in rows if r["vehicle"].pk == _int_or_none(vehicle_id)),
            None,
        )
        try:
            session, raw_token = services.start_session(
                vehicle,
                by=request.user,
                cap_hours=_parse_cap_hours(request.POST.get("cap_hours")),
                now=now,
            )
        except ValidationError as exc:
            _flash_error(request, exc)
            raw_token = None
        else:
            messages.success(request, _("Session started — publishing live."))
    return render(
        request,
        "tracking/start.html",
        {"vehicle_rows": rows, "raw_token": raw_token, "cap_min": _CAP_MIN, "cap_max": _CAP_MAX},
    )


def _int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@login_required
@require_POST
def session_end(request, pk: int):
    """Crew or SACCO-admin STOP while live (the service refuses late ends)."""
    session = _my_session(request, pk)
    try:
        services.end_session(session, by=request.user, now=timezone.now())
    except ValidationError as exc:
        _flash_error(request, exc)
    else:
        messages.info(request, _("Session ended — publishing stopped."))
    return redirect("tracking:my_sessions")


@login_required
@require_POST
def session_rotate(request, pk: int):
    """Rotate the token (old dead). Renders the display-once page with the
    NEW raw token, straight from the POST response."""
    session = _my_session(request, pk)
    try:
        raw_token = services.rotate_token(
            session, by=request.user, now=timezone.now()
        )
    except ValidationError as exc:
        _flash_error(request, exc)
        return redirect("tracking:my_sessions")
    messages.success(request, _("Token rotated — the old one no longer works."))
    return render(
        request,
        "tracking/token.html",
        {"session": session, "raw_token": raw_token, "rotated": True},
    )


@login_required
def my_sessions(request):
    """The crew page: sessions on vehicles where THEY are approved crew —
    derived status + privacy-filtered position counts (the count the map
    could show — never raw rows)."""
    from apps.routes import crew as routes_crew

    rows = []
    for vehicle in routes_crew.vehicles_of_crew(request.user):
        sessions = vehicle.tracking_sessions.order_by("-started_at")
        rows.append({"vehicle": vehicle, "sessions": list(sessions)})
    _decorate(request, rows)
    return render(request, "tracking/my_sessions.html", {"rows": rows})


@login_required
def operator_sessions(request):
    """The operator page: sessions on THEIR saccos' vehicles only — the
    tenant wall via services.visible_vehicles (staff included = no bypass)."""
    from apps.fleet import services as fleet_services

    rows = []
    now = timezone.now()
    for vehicle in fleet_services.visible_vehicles(request.user):
        sessions = vehicle.tracking_sessions.order_by("-started_at")
        rows.append({"vehicle": vehicle, "sessions": list(sessions)})
    _decorate(request, rows)
    return render(request, "tracking/operator_sessions.html", {"rows": rows})


def _decorate(request, rows):
    """Attach derived status + the privacy-filtered position count.

    WHY recompute per row in the view layer: status is DERIVED (no stored
    column to trust); the count comes through visible_positions so it is
    the SUPPRESSED count — these pages literally cannot show what the
    query layer hides."""
    now = timezone.now()
    status_of = services.session_status
    for row in rows:
        for session in row["sessions"]:
            session.derived_status = status_of(session, now=now)
            session.visible_count = services.visible_positions(
                session, now=now
            ).count()
