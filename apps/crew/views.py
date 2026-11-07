"""views.py — the crew phone's ONE lightweight trip page (Prompt 29).

WHAT this is: four honest states on a single page, driven ONLY by the
siblings' PUBLIC doors (routes.crew + tracking.services) — the app is
stateless (no models): it stores no location data of its own; 27's tables
are the server of record and 28's ingest is the only sink the phone sends to.

    NOT_AUTHORIZED    an APPROVED seat is needed — honest copy + link to
                      fleet:crew_mine (fleet's approval flow); NO start form
                      (no fake start button)
    AUTHORIZED_IDLE   pick vehicle (+ optional route/direction from the
                      fleet doors) and SEE the cost facts before starting
    PUBLISHING        a 27 session is LIVE: "Publishing …" indicator with
                      the data-used counter + End trip. Position publishes
                      ONLY while the session is authorized — the DPA state,
                      visible, is part of the page
    ENDED             honest summary: the server-stored ping count and the
                      still-held device queue are real facts; device-side
                      sent/failed counts render "—" (see the token rule)

THE 28 SEAM — documented provisional contract, mocked in my tests; the
parent binds these constants to 28's real endpoint at consolidation:

    INGEST_URL_TEMPLATE = "/tracking/ingest/{session_id}/"
    INGEST_HEADER       = "X-Session-Token"
    body    {"vehicle_id": <int>, "pings": [{"recorded_at": ISO, "lat":
             <float>, "lon": <float>, "speed_kmh": <int>}, ...]}  batch ≤ 50
    reply   {"accepted": true|false, "duplicates": <int>, "downgraded": <int>}

TOKEN RULE (display-once, Prompt 27): the START POST response renders the
raw token ONCE (data-test="raw-token"), read by the inline JS into a
variable of THAT render's memory — never a cookie, never localStorage, so
a reload renders PUBLISHING minus token and that tab cannot send pings;
it says so. WHY: a lingering credential is the leak the display-once rule
exists to prevent; the cost (one dead tab per reload) is shown honestly.

PROGRESSIVE ENHANCEMENT: start/end are real <form> POSTs and fully work
with JavaScript disabled; JS only adds geolocation capture, the queue,
batching, counters, and connectivity retry (all render as "—" without it).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.routes import crew as routes_crew
from apps.tracking import services
from apps.tracking.models import TrackingSession

# ------------------------------------------------------------ the 28 SEAm
INGEST_URL_TEMPLATE = "/tracking/ingest/{session_id}/"  # SEAM-VALUE (28's URL)
INGEST_HEADER = "X-Session-Token"                       # SEAM-VALUE (28's hdr)
INGEST_BATCH_MAX = 50   # 28's documented batch ceiling — batch, don't stream
# Device-side queue cap the page mirrors (data-buffer-max). Overflow drops
# the OLDEST fix with a VISIBLE notice — never silent (integrity pins).
BUFFER_MAX = 200
# Versioned localStorage key: "v1" in the name so a later format change
# becomes a v2 — old buffers are never silently re-read by new logic.
STORAGE_KEY = "mtaani.crew.trip.v1"
POLL_SECONDS = 60  # the cost-facts copy and the JS tunable agree on this

# Page-only pick vocabulary (display). NOTE: route/direction are crew UI
# context — 27's session and 28's documented body carry neither, so nothing
# is fabricated INTO a payload that doesn't model it (no invented fields).
DIRECTIONS = ("FORWARD", "REVERSE")


class PageState:
    """The page-state vocabulary — data-state attr for JS, template selects."""

    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    AUTHORIZED_IDLE = "AUTHORIZED_IDLE"
    PUBLISHING = "PUBLISHING"
    ENDED = "ENDED"


def _ingester() -> dict:
    """SEAM + JS config the page reads from data-attributes. Single source:
    THIS module's constants; tests pin the rendered attribute values."""
    return {
        "url": INGEST_URL_TEMPLATE,
        "header": INGEST_HEADER,
        "batch_max": INGEST_BATCH_MAX,
        "buffer_max": BUFFER_MAX,
        "storage_key": STORAGE_KEY,
        "poll_seconds": POLL_SECONDS,
    }


def _ended_row(request, vehicles) -> dict | None:
    """ENDED summary from ?ended=<pk>, ONLY for a vehicle the crew serves.

    WHY the tenancy filter: a forged pk must not render another crew's
    summary; an unknown/foreign pk falls through honestly (idle), never a
    leak, never a crash."""
    raw = (request.GET.get("ended") or "").strip()
    if not raw.isdigit():
        return None
    session = (
        TrackingSession.objects.filter(pk=int(raw), vehicle__in=vehicles)
        .select_related("vehicle")
        .first()
    )
    if session is None:
        return None
    return {
        "session_pk": session.pk,
        "vehicle_id": session.vehicle_id,
        "plate": session.vehicle.plate,
        "nickname": session.vehicle.nickname,
        "pings_stored": session.pings.count(),  # server of record (27's)
    }


def _live_row(session, vehicle, *, now) -> dict:
    """One live-session display row — display facts only, NO coordinates."""
    return {
        "session_pk": session.pk,
        "vehicle_id": session.vehicle_id,
        "plate": vehicle.plate,
        "nickname": vehicle.nickname,
        # The cap deadline is an authorization fact, shown to the hour:
        "until": timezone.localtime(session.deadline()).strftime("%H:%M"),
        "token_once": False,   # flipped True only by the ONE-TIME render
        "raw_token": "",       # filled once by trip_start (display-once)
    }


def _page_state(request) -> tuple[str, dict]:
    """(state, context) — the ONE state machine shared by the page render
    AND the status JSON.

    WHY shared: page and status partial must never disagree about the trip's
    state (a lying publishing indicator is the DPA failure mode)."""
    now = timezone.now()
    ingester = _ingester()
    if not routes_crew.is_approved_crew(request.user):
        return PageState.NOT_AUTHORIZED, {"ingester": ingester}
    vehicles = routes_crew.vehicles_of_crew(request.user).prefetch_related("routes")

    # LIVE sessions first (any of my vehicles) — the page publishes one:
    live_rows = []
    for vehicle in vehicles:
        session = services.live_session_for(vehicle, now=now)
        if session is not None:
            live_rows.append(_live_row(session, vehicle, now=now))
    if live_rows:
        return PageState.PUBLISHING, {
            "ingester": ingester,
            "live_rows": live_rows,
            "live": live_rows[0],  # the JS binds to THIS row's session
        }

    ended = _ended_row(request, vehicles)
    if ended is not None:
        return PageState.ENDED, {
            "ingester": ingester,
            "ended": ended,
            "cap_hours": services.DEFAULT_CAP_HOURS,
        }

    # Idle: honest pick lists. Routes come THROUGH the fleet M2M vehicles
    # already hold (fleet's door) — no direct route-table reach.
    routes, seen = [], set()
    for vehicle in vehicles:
        for route in vehicle.routes.all():
            if route.pk not in seen:
                seen.add(route.pk)
                routes.append(route)
    return PageState.AUTHORIZED_IDLE, {
        "ingester": ingester,
        "vehicle_rows": [
            {"vehicle_id": v.pk, "plate": v.plate, "nickname": v.nickname}
            for v in vehicles
        ],
        "routes": routes,
        "cap_hours": services.DEFAULT_CAP_HOURS,
    }


# ------------------------------------------------------------------- pages
@login_required
def trip(request):
    """The page (GET). Anonymous → login; non-crew → the honest refusal
    state; else idle / publishing / ended — all one template."""
    state, ctx = _page_state(request)
    return render(request, "crew/trip.html", {"state": state, **ctx})


@login_required
@require_GET
def trip_status(request):
    """The status partial (JSON) a reloaded tab/phone reads: the SAME state
    machine as the page, exact key set pinned in tests."""
    state, ctx = _page_state(request)
    row = ctx.get("live") or ctx.get("ended") or {}
    return JsonResponse(
        {
            "state": state,
            "session_id": row.get("session_pk"),
            "vehicle_id": row.get("vehicle_id"),
            "plate": row.get("plate", ""),
            "pings_stored": (ctx["ended"]["pings_stored"] if "ended" in ctx else None),
        }
    )


@login_required
@require_POST
def trip_start(request):
    """Start a trip — through P27's PUBLIC door (services.start_session)
    ONLY; the raw token goes into THIS response's ONE-TIME render.

    The crew page is crew-specific by design: plain members, pending
    applicants and SACCO admins WITHOUT a crew seat are refused here
    (403) — the tracking:start surface is the operator equivalent."""
    if not routes_crew.is_approved_crew(request.user):
        raise PermissionDenied
    vehicles = list(
        routes_crew.vehicles_of_crew(request.user).prefetch_related("routes")
    )
    want = (request.POST.get("vehicle") or "").strip()
    vehicle = next((v for v in vehicles if str(v.pk) == want), None)
    if vehicle is None:
        # Missing or foreign vehicle — refuse, no probing of other SACCOs.
        raise PermissionDenied
    direction = (request.POST.get("direction") or "").strip().upper() or DIRECTIONS[0]
    if direction not in DIRECTIONS:
        messages.error(request, _("Pick a direction from the list."))
        return redirect("crew:trip")
    route_raw = (request.POST.get("route") or "").strip()
    served = {route.pk for route in vehicle.routes.all()}
    if route_raw and (not route_raw.isdigit() or int(route_raw) not in served):
        messages.error(request, _("Pick a route that this vehicle actually serves."))
        return redirect("crew:trip")
    try:
        session, raw_token = services.start_session(vehicle=vehicle, by=request.user)
    except ValidationError as exc:
        # Honest refusals (e.g. one-live-per-vehicle) surface verbatim.
        messages.error(request, "; ".join(exc.messages))
        return redirect("crew:trip")

    state, ctx = _page_state(request)
    if state == PageState.PUBLISHING:
        for row in ctx["live_rows"]:
            if row["session_pk"] == session.pk:
                row["token_once"] = True
                row["raw_token"] = raw_token
                ctx["live"] = row  # THIS render's JS binds to THE new session
                break
    return render(request, "crew/trip.html", {"state": state, **ctx})


@login_required
@require_POST
def trip_end(request, session_id: int):
    """Crew STOP through 27's door, then back to the page honestly ENDED.

    Tenancy BEFORE the service call: a row outside the requester's crew
    vehicles (or a nonexistent pk) is 403 — the same authorization_for
    door the service itself enforces, so the page cannot race a lie."""
    session = (
        TrackingSession.objects.filter(pk=session_id)
        .select_related("vehicle")
        .first()
    )
    if session is None:
        raise PermissionDenied
    if services.authorization_for(request.user, session.vehicle) is None:
        raise PermissionDenied
    try:
        session = services.end_session(session, by=request.user)
    except ValidationError as exc:
        # Refusals (double end, past-cap) are honest info, never a rewrite.
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.info(request, _("Trip ended — publishing stopped."))
    return redirect(f"{reverse('crew:trip')}?ended={session.pk}")
