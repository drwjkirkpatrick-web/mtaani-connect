"""services.py — follow CRUD, nickname resolution, alert scans (Prompt 31).

THE PUBLIC API FOR OTHER APPS + FUTURE CRON (cross-app/stateful logic lives
HERE once):

    resolve_vehicle_by_nickname(q) -> Vehicle
        unique ACTIVE vehicle by nickname (exact-first, then contains);
        raises AmbiguousNickname / NicknameNotFound with plain messages
    follow_vehicle(*, user, nickname) -> VehicleFollow   (duplicate refused)
    unfollow_vehicle(vehicle, *, user) -> bool           (idempotent)
    follow_route(*, user, route) -> RouteFollow          (duplicate refused)
    unfollow_route(route, *, user) -> bool               (idempotent)
    toggle_notify(follow) -> bool     one tap: flips notify_enabled
    set_alert_stage(follow, stage)     validates the stage lies on the path
    set_radius(follow, km)             validates the 1–10 km bounds
    check_and_notify(follows, *, now, adapter=None) -> list[sends]
        ONE follow row (or iterable) → approach check → notify ONCE per
        approach event (30-min dedupe window, the ledger enforces it)
    alert_scan(*, now=None, adapter=None) -> list[sends]
        cron-callable sweep over EVERY opted-in follow (deterministic:
        pk-ordered, injected clock, one adapter pass)
    stage_arrivals(route, stage, *, user, now=None) -> rows
        the arrivals PAGE/JSON payload: followed vehicles' ETAs + honest
        silences — "my matatu is coming" works or admits it cannot tell.

PRIVACY/DATA DISCIPLINE: positions enter only through Prompt 27's door
(eta.last_visible_ping / visible_positions); the alert service never
touches a raw PingPoint manager. Fleet/status reads go through fleet's
public vocabulary (VehicleStatus) + the M2M the brief makes public.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from . import eta, notify
from .models import (
    ALERT_DEDUPE_MINUTES,
    MAX_RADIUS_KM,
    MIN_RADIUS_KM,
    AlertFollowKind,
    FollowAlert,
    RouteFollow,
    VehicleFollow,
)

# ------------------------------------------------------------------ messages
MSG_ALREADY_FOLLOWING = _("You already follow %(name)s.")
MSG_UNFOLLOWED = _("Unfollowed %(name)s.")
MSG_NOTIFIED_ON = _("Notifications switched ON for %(name)s.")
MSG_NOTIFIED_OFF = _("Notifications switched OFF for %(name)s.")
MSG_STAGE_SET = _("Alert stage set to %(stage)s.")
MSG_RADIUS_SET = _("Alert radius set to %(km)s km (1–10).")
MSG_BAD_RADIUS = _("Radius must be a whole number between 1 and 10 km.")
MSG_FOLLOWED = _(
    "Now following %(name)s — pick an alert stage and switch notifications "
    "on when ready."
)
MSG_FOLLOW_REMOVED = _("%(name)s removed from your follows.")

MSG_NO_VEHICLE = _(
    "No active matatu found for that nickname — check the spelling."
)
MSG_AMBIGUOUS = _(
    "“%(q)s” matches more than one active matatu — use the plate to pick one."
)


class NicknameNotFound(Exception):
    """No unique ACTIVE vehicle for that nickname (message is display-ready)."""


class AmbiguousNickname(Exception):
    """More than one ACTIVE vehicle matches (message is display-ready)."""


# ----------------------------------------------------- nickname → vehicle
def resolve_vehicle_by_nickname(nickname: str):
    """Resolve a nickname to ONE unique ACTIVE vehicle — or refuse honestly.

    Matching (spec): exact case-insensitive nickname first; a unique
    CONTAINS match second; anything ambiguous (multiple ACTIVE vehicles
    sharing that nickname) is REFUSED with a plain rider message — the app
    never guesses which matatu the rider meant. Retired metal never
    resolves (fleet.services.active_vehicles is the public registry door).
    """
    from apps.fleet import services as fleet_services  # lazy: registry order

    q = (nickname or "").strip()
    if not q:
        raise NicknameNotFound(MSG_NO_VEHICLE)
    active = fleet_services.active_vehicles()
    exact = list(active.filter(nickname__iexact=q))
    if len(exact) > 1:
        raise AmbiguousNickname(MSG_AMBIGUOUS % {"q": q})
    if exact:
        return exact[0]
    fuzzy = list(active.filter(nickname__icontains=q))
    if len(fuzzy) > 1:
        raise AmbiguousNickname(MSG_AMBIGUOUS % {"q": q})
    if fuzzy:
        return fuzzy[0]
    raise NicknameNotFound(MSG_NO_VEHICLE)


def vehicle_display(vehicle) -> str:
    """"Mwangaza (KCB 220P)"-style — nickname first (riders think in names)."""
    return f"{vehicle.nickname} ({vehicle.plate})" if vehicle.nickname else vehicle.plate


# ----------------------------------------------------------------- CRUD
def follow_vehicle(*, user, nickname: str) -> VehicleFollow:
    """Follow ONE matatu by nickname (duplicate refused — spec + unique)."""
    vehicle = resolve_vehicle_by_nickname(nickname)
    if VehicleFollow.objects.filter(user=user, vehicle=vehicle).exists():
        raise ValidationError(
            MSG_ALREADY_FOLLOWING % {"name": vehicle_display(vehicle)}
        )
    follow = VehicleFollow(user=user, vehicle=vehicle)  # notify defaults OFF
    follow.full_clean()  # radius bounds + stage-path rules (stage None here)
    follow.save()
    return follow


def unfollow_vehicle(*, user, vehicle) -> bool:
    """Drop a vehicle follow. Idempotent: False when it wasn't there."""
    deleted, _row_type = VehicleFollow.objects.filter(
        user=user, vehicle=vehicle
    ).delete()
    return deleted > 0


def follow_route(*, user, route) -> RouteFollow:
    """Follow ONE route (duplicate refused). Notifications stay OFF."""
    if RouteFollow.objects.filter(user=user, route=route).exists():
        raise ValidationError(MSG_ALREADY_FOLLOWING % {"name": route.name_en})
    follow = RouteFollow(user=user, route=route)
    follow.full_clean()
    follow.save()
    return follow


def unfollow_route(*, user, route) -> bool:
    deleted, _row_type = RouteFollow.objects.filter(user=user, route=route).delete()
    return deleted > 0


# ---------------------------------------------------- preference mutations
def toggle_notify(follow) -> bool:
    """The ONE-TAP consent switch: flips notify_enabled, returns new state.

    WHY a plain flip (not a form): the spec demands "mutable in one tap" —
    the rider's decision is recorded as either on or off, nothing grey.
    """
    follow.notify_enabled = not follow.notify_enabled
    follow.save(update_fields=["notify_enabled", "updated_at"])
    return follow.notify_enabled


def set_alert_stage(follow, stage) -> None:
    """Set/reset the follow's alert stage; the model's clean() refuses a
    stage that does not lie on the followed path."""
    follow.notify_stage = stage
    follow.full_clean()  # stage-on-path validation lives on the model
    follow.save(update_fields=["notify_stage", "updated_at"])


def set_radius(follow, radius_km) -> None:
    """Set the approach radius (1–10 km; everything else refused)."""
    if isinstance(radius_km, bool) or not isinstance(radius_km, int):
        raise ValidationError(MSG_BAD_RADIUS)
    if not (MIN_RADIUS_KM <= radius_km <= MAX_RADIUS_KM):
        raise ValidationError(MSG_BAD_RADIUS)
    follow.radius_km = radius_km
    follow.full_clean()
    follow.save(update_fields=["radius_km", "updated_at"])


# ------------------------------------------------------------- alert checks
def _active_vehicles_for_follow(follow) -> list:
    """The vehicles a follow can alert about, deterministically ordered.

    RouteFollow: every ACTIVE vehicle on the route (route.vehicles M2M);
    VehicleFollow: that vehicle alone. ACTIVE filter via fleet's status
    vocabulary — retired metal raises nothing, it just never appears.
    """
    from apps.fleet.models import VehicleStatus  # lazy: registry order

    if isinstance(follow, RouteFollow):
        return list(
            follow.route.vehicles.filter(status=VehicleStatus.ACTIVE.value)
            .order_by("plate")
        )
    if isinstance(follow, VehicleFollow):
        if follow.vehicle.status != VehicleStatus.ACTIVE.value:
            return []
        return [follow.vehicle]
    return []


def _recent_alert_pk(follow, stage, session, *, now) -> int | None:
    """The pk of a ledger row for the SAME (follow, stage, session) inside
    the dedupe window — the ONE-notification-per-event memory. Boundary:
    the window is EXCLUSIVE at its edge (alert at exactly 30 min ago does
    not block), matching the siblings' settled boundary style."""
    window_start = now - timedelta(minutes=ALERT_DEDUPE_MINUTES)
    kind = (
        AlertFollowKind.VEHICLE
        if isinstance(follow, VehicleFollow)
        else AlertFollowKind.ROUTE
    )
    base = FollowAlert.objects.filter(
        follow_kind=kind.value,
        stage=stage,
        session=session,
        notified_at__gt=window_start,
    )
    if isinstance(follow, VehicleFollow):
        return (
            base.filter(vehicle_follow=follow)
            .values_list("pk", flat=True)
            .first()
        )
    return base.filter(route_follow=follow).values_list("pk", flat=True).first()


def _send_and_record(follow, stage, session, *, estimate, now, adapter) -> dict:
    """Compose the message, push it through the adapter and write the ledger.

    The body carries the ETA range ONLY when the arithmetic had enough
    data — a push that says "approaching, about 12–20 min" never fabricates
    minutes it does not have; without data it stays honest ("approaching")
    while the page/JSON keeps the explicit insufficient-data line.
    """
    vehicle = session.vehicle
    label = vehicle.nickname or vehicle.plate
    title = _("Your matatu is approaching %(stage)s") % {"stage": stage.name}
    if estimate:
        body = _(
            "%(name)s (%(plate)s) is approaching %(stage)s — about %(range)s."
        ) % {
            "name": label,
            "plate": vehicle.plate,
            "stage": stage.name,
            "range": eta.format_range(estimate),
        }
    else:
        body = _("%(name)s (%(plate)s) is approaching %(stage)s.") % {
            "name": label,
            "plate": vehicle.plate,
            "stage": stage.name,
        }
    receipt = adapter.send(follow, title, body)
    is_vehicle = isinstance(follow, VehicleFollow)
    FollowAlert.objects.create(
        follow_kind=(
            AlertFollowKind.VEHICLE.value
            if is_vehicle
            else AlertFollowKind.ROUTE.value
        ),
        vehicle_follow=follow if is_vehicle else None,
        route_follow=None if is_vehicle else follow,
        stage=stage,
        session=session,
        title=str(title),
        body=str(body),
        adapter_id=receipt or "",
        notified_at=now,
    )
    return {
        "follow_kind": (
            AlertFollowKind.VEHICLE.value
            if is_vehicle
            else AlertFollowKind.ROUTE.value
        ),
        "follow_id": follow.pk,
        "vehicle_id": vehicle.pk,
        "stage_id": stage.pk,
        "session_id": session.pk,
        "title": str(title),
        "body": str(body),
        "adapter_id": receipt,
        # estimate travels WITH its confidence label (payload contract).
        "estimate": eta.estimate_payload(*estimate) if estimate else None,
        "notified_at": now.isoformat(),
    }


def check_follow(follow, *, now, adapter) -> dict | None:
    """One follow row: approach check → ONE send at most (or None).

    Semantics (decisions, documented):
      * notify ENABLED only — opt-out rows produce NOTHING, ever;
      * a chosen alert stage required — a follow without one raises no
        approach alerts (honest: no invented trigger);
      * the trigger is the LAST VISIBLE ping (27's door) within the row's
        own radius of the stage (boundary-inclusive);
      * dedupe: a send for the same (follow, stage, SESSION) within the
        30-min window is suppressed.
    """
    if not follow.notify_enabled:
        return None
    stage = follow.notify_stage
    if stage is None:
        return None
    from apps.tracking import services as tracking_services  # lazy: registry

    for vehicle in _active_vehicles_for_follow(follow):
        session = tracking_services.live_session_for(vehicle, now=now)
        if session is None:
            continue
        if not eta.approaching_stage(
            session, stage, radius_km=follow.radius_km, now=now
        ):
            continue
        if _recent_alert_pk(follow, stage, session, now=now) is not None:
            continue  # same approach event already announced — stay quiet
        estimate = eta.eta_minutes(session, stage, now=now)
        return _send_and_record(
            follow, stage, session, estimate=estimate, now=now, adapter=adapter
        )
    return None


def check_and_notify(follows, *, now, adapter=None) -> list[dict]:
    """check_and_notify(follow rows, *, now, adapter=None) — the spec door.

    `now` is REQUIRED here (injected clock); adapter None → the registry
    default (fresh MockPush). Returns the SENS that actually happened.
    """
    now = timezone.now() if now is None else now
    if adapter is None:
        adapter = notify.get_adapter()
    results = []
    for follow in follows:
        result = check_follow(follow, now=now, adapter=adapter)
        if result is not None:
            results.append(result)
    return results


def alert_scan(*, now=None, adapter=None) -> list[dict]:
    """Cron-callable sweep over EVERY opted-in follow (deterministic).

    Order of work is PINNED (pk-ascending vehicle follows, then route
    follows) so two scans at the same injected clock with fresh adapters
    return identical lists — the determinism contract tests rely on it.
    `now` injected; None = wall clock (real cron use).
    """
    now = timezone.now() if now is None else now
    rows: list = list(
        VehicleFollow.objects.filter(notify_enabled=True)
        .select_related("vehicle", "notify_stage")
        .order_by("pk")
    ) + list(
        RouteFollow.objects.filter(notify_enabled=True)
        .select_related("route", "notify_stage")
        .order_by("pk")
    )
    return check_and_notify(rows, now=now, adapter=adapter)


# ------------------------------------------------------- arrivals payloads
def _arrival_row(vehicle, session, stage, *, radius_km, now) -> dict:
    """One followed vehicle's honest arrival state at (route, stage)."""
    from apps.core.geo import distance_km
    from apps.tracking import services as tracking_services  # lazy: registry

    live = session is not None
    row = {
        "vehicle_id": vehicle.pk,
        "plate": vehicle.plate,
        "nickname": vehicle.nickname,
        "session_id": session.pk if live else None,
        "live": live,
        "distance_km": None,
        "approaching": False,
        "estimate": None,
        "estimate_text": None,
        # Status line the UI renders INSTEAD of a number (never 0/∞).
        "estimate_status": (
            eta.MSG_INSUFFICIENT_DATA if live else eta.MSG_NOT_TRACKING
        ),
    }
    if not live:
        return row  # honest "not tracking right now" — no numbers at all
    last = eta.last_visible_ping(session, now=now)
    if last is not None:
        row["distance_km"] = distance_km(
            (float(last.lat), float(last.lon)), stage.coordinate
        ) if stage.coordinate is not None else None
    row["approaching"] = eta.approaching_stage(
        session, stage, radius_km=radius_km, now=now
    )
    estimate = eta.eta_minutes(session, stage, now=now)
    if estimate is not None:
        row["estimate"] = eta.estimate_payload(*estimate)  # label INSIDE payload
        row["estimate_text"] = eta.format_range(estimate)
        row["estimate_status"] = None  # silence resolved honestly
    return row


def stage_arrivals(route, stage, *, user, now=None) -> dict:
    """The acceptance surface: followed vehicles' arrivals at stage+route.

    Rows come from the RIDER's OWN follows (vehicle follows on this route +
    her follows of this route), deduped by vehicle; the radius is the
    vehicle-follow's own when she has one, else the route-follow's. Rows
    with no live session stay in the list with MSG_NOT_TRACKING — silence
    is data-honest, absence of the row would be a lie.
    """
    from apps.fleet.models import VehicleStatus  # lazy: registry order
    from apps.tracking import services as tracking_services  # lazy: registry

    now = timezone.now() if now is None else now
    rows: dict[int, dict] = {}
    vehicle_follows = {
        vf.vehicle_id: vf
        for vf in VehicleFollow.objects.filter(user=user, vehicle__routes=route)
        .filter(vehicle__status=VehicleStatus.ACTIVE.value)
        .select_related("vehicle", "notify_stage")
    }
    route_follow_here = RouteFollow.objects.filter(user=user, route=route).first()
    radius_for = {pk: vf.radius_km for pk, vf in vehicle_follows.items()}
    if route_follow_here is not None:
        for vehicle in (
            route.vehicles.filter(status=VehicleStatus.ACTIVE.value)
            .order_by("plate")
        ):
            radius_for.setdefault(vehicle.pk, route_follow_here.radius_km)
    for pk in sorted(radius_for):  # deterministic plate-independent order (pk)
        vehicle = None
        if pk in vehicle_follows:
            vehicle = vehicle_follows[pk].vehicle
        else:
            vehicle = route.vehicles.filter(
                pk=pk, status=VehicleStatus.ACTIVE.value
            ).first()
        session = tracking_services.live_session_for(vehicle, now=now)
        row = _arrival_row(
            vehicle,
            session,
            stage,
            radius_km=radius_for[pk],
            now=now,
        )
        rows[pk] = row
    return {
        "route": route,
        "stage": stage,
        "rows": [rows[pk] for pk in sorted(rows)],
    }
