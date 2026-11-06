"""Fleet services — the single write door + the query-layer tenant wall (26).

SIBLING-INTEGRATION CONTRACT (read before building on the transport side):

    from apps.fleet import services

    visible_vehicles(user)       # ONLY vehicles of saccos `user` administers
    is_sacco_admin(user, sacco)
    saccos_of_admin(user)
    crew_role_of(user)           # 'driver' | 'conductor' | None
    is_approved_crew(user)       # True ⇔ ≥1 APPROVED crew seat
    vehicles_of_crew(user)       # ACTIVE vehicles with APPROVED seats

    register_vehicle(...) / retire_vehicle(...)
    request_crew(...) / approve_crew(...) / revoke_crew(...)
    grant_sacco_admin(...)
    search_vehicles(q)           # plate-prefix OR nickname/route-name search

TENANT ISOLATION AT THE QUERY LAYER, NOT THE TEMPLATE: every operator read
or write builds its queryset from ``visible_vehicles(user)``. A SACCO A
admin's queryset STRUCTURALLY contains no SACCO B vehicle, so no view tweak,
forged URL or crafted POST can reach B's rows through operator pages.
NOTE: platform staff get NO bypass here — staff work happens through the
Django admin (which is platform-level by design), never through a SACCO's
operator pages. Tested in tests/test_fleet.py.

KDPA: plates + nicknames are business data (searchable by design); this
module stores no personal contact info anywhere, and audit rows keep who/
when/what without phone numbers.

Accountability pitfall (PROMPTS.md 26) restated as rules in code:
  * a crew seat grants position-publishing authority ONLY (Prompt 27
    consumes is_approved_crew/vehicles_of_crew — those two functions are
    the only authority surface);
  * it NEVER mutes ordinary-user obligations — reporting duties, rate
    limits and blocks apply unchanged (tests pin moderation's
    report_content rate limit and is_moderator == False for crew users).
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.moderation.services import is_moderator
from apps.routes.models import Route, Sacco

from .models import (
    CrewMember,
    CrewRole,
    CrewStatus,
    FleetAction,
    FleetAudit,
    SaccoAdmin,
    Vehicle,
    VehicleStatus,
)

SEARCH_LIMIT = 20  # rendered rows per list surface (hard rule)


def _now(now):
    return timezone.now() if now is None else now


def _audit(*, action: str, actor, sacco=None, vehicle=None, crew=None, note: str = ""):
    """Append one immutable operator trail row (returns it for tests)."""
    return FleetAudit.objects.create(
        action=action, actor=actor, sacco=sacco, vehicle=vehicle,
        crew=crew, note=(note or "")[:240],
    )


# ------------------------------------------------------------------ tenancy
def saccos_of_admin(user) -> models.QuerySet[Sacco]:
    """Saccos where `user` is an operator/admin — [] for anonymous."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Sacco.objects.none()
    return Sacco.objects.filter(admins__user=user).order_by("name")


def is_sacco_admin(user, sacco) -> bool:
    """True ⇔ `user` sits on THIS sacco's admin table."""
    if user is None or sacco is None:
        return False
    if not getattr(user, "is_authenticated", False):
        return False
    return SaccoAdmin.objects.filter(sacco=sacco, user=user).exists()


def visible_vehicles(user) -> models.QuerySet[Vehicle]:
    """THE tenant wall: vehicles of saccos `user` administers — nothing else.

    Deliberately returns .none() for anonymous, logged-out, ordinary users
    AND platform staff (staff uses /admin/). Prompts 27/29 MUST funnel their
    operator surfaces through this function instead of re-filtering.
    """
    return (
        Vehicle.objects.filter(sacco__in=saccos_of_admin(user))
        .select_related("sacco")
        .prefetch_related("routes")
        .order_by("plate")
    )


# ------------------------------------------------------------------ vehicles
@transaction.atomic
def register_vehicle(
    *, sacco, raw_plate: str, capacity: int, nickname: str = "",
    routes=None, actor, now=None,
) -> Vehicle:
    """Register a vehicle FOR a sacco the actor administers (human gate)."""
    if not is_sacco_admin(actor, sacco):
        raise ValidationError(
            _("Only a SACCO admin of this SACCO can register its vehicles.")
        )
    vehicle = Vehicle(
        plate=(raw_plate or ""),
        nickname=(nickname or "").strip(),
        capacity=capacity,
        sacco=sacco,
        status=VehicleStatus.ACTIVE.value,
    )
    vehicle.full_clean()  # plate format/normalize + capacity bounds + dupe
    vehicle.save()
    if routes:
        vehicle.routes.set(
            Route.objects.filter(pk__in=[r.pk for r in routes])
        )
    _audit(action=FleetAction.VEHICLE_REGISTERED, actor=actor, sacco=sacco,
           vehicle=vehicle)
    return vehicle


@transaction.atomic
def update_vehicle_routes(vehicle, *, route_pks=None, actor) -> Vehicle:
    """Set the operated route list (ids must exist; unknown ids drop out)."""
    if not is_sacco_admin(actor, vehicle.sacco):
        raise ValidationError(
            _("Only a SACCO admin of this SACCO can edit its vehicles.")
        )
    ids = [int(pk) for pk in (route_pks or []) if str(pk).isdigit()]
    vehicle.routes.set(Route.objects.filter(pk__in=ids))
    vehicle.save(update_fields=["updated_at"])
    _audit(action=FleetAction.VEHICLE_UPDATED, actor=actor, vehicle=vehicle,
           sacco=vehicle.sacco, note=f"{vehicle.routes.count()} route(s)")
    return vehicle


@transaction.atomic
def retire_vehicle(vehicle, *, actor, now=None) -> Vehicle:
    """ACTIVE → RETIRED: parked metal cannot carry authority forward."""
    if not is_sacco_admin(actor, vehicle.sacco):
        raise ValidationError(
            _("Only a SACCO admin of this SACCO can retire its vehicles.")
        )
    if vehicle.status == VehicleStatus.RETIRED.value:
        raise ValidationError(_("This vehicle is already retired."))
    vehicle.status = VehicleStatus.RETIRED.value
    vehicle.save(update_fields=["status", "updated_at"])
    _audit(action=FleetAction.VEHICLE_RETIRED, actor=actor, sacco=vehicle.sacco,
           vehicle=vehicle)
    return vehicle


def active_vehicles() -> models.QuerySet[Vehicle]:
    """Public registry view (plates/nicknames are business data): ACTIVE row."""
    return (
        Vehicle.objects.filter(status=VehicleStatus.ACTIVE.value)
        .select_related("sacco")
        .prefetch_related("routes")
        .order_by("plate")
    )


def search_vehicles(q: str, *, limit: int = SEARCH_LIMIT) -> models.QuerySet[Vehicle]:
    """Plate / nickname / route-name search over the PUBLIC registry.

    v1 discipline: case-insensitive CONTAINS across plate + nickname +
    route names (en/sw). WHY not trigram yet: correct-before-fast; the
    pg_trgm GIN upgrade (routes.search pattern) is optional later, and
    swapping this single functions internals keeps every caller intact.
    """
    q = (q or "").strip()
    if not q:
        return Vehicle.objects.none()
    return (
        active_vehicles()
        .filter(
            Q(plate__icontains=q)
            | Q(nickname__icontains=q)
            | Q(routes__name_en__icontains=q)
            | Q(routes__name_sw__icontains=q)
        )
        .distinct()  # M2M join multiplies rows — one vehicle per result
        .order_by("plate")[:limit]
    )


# ------------------------------------------------------------------ crew
@transaction.atomic
def request_crew(*, user, vehicle, role: str, now=None) -> CrewMember:
    """A member asks for a crew seat: row lands PENDING — authority NOT yet.

    Idempotent: a second request while pending returns the same row; a
    request after revocation REOPENS the same row as PENDING (the seat's
    history is preserved; approval is never re-granted automatically).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        raise ValidationError(_("Log in to request a crew seat."))
    if role not in (CrewRole.DRIVER.value, CrewRole.CONDUCTOR.value):
        raise ValidationError(_("Pick a crew role: driver or conductor."))
    if vehicle is None or not vehicle.pk:
        raise ValidationError(_("A crew seat needs a vehicle."))
    if vehicle.status != VehicleStatus.ACTIVE.value:
        raise ValidationError(_("Crew is requested for active vehicles only."))
    seat, _created = CrewMember.objects.get_or_create(
        user=user, vehicle=vehicle, role=role,
        defaults={
            "status": CrewStatus.PENDING.value,
            "approved_by": None, "approved_at": None,
            "revoked_by": None, "revoked_at": None,
        },
    )
    if seat.status == CrewStatus.APPROVED.value:
        raise ValidationError(
            _("You are already approved crew on this vehicle in this role.")
        )
    reopened = None
    if seat.status == CrewStatus.REVOKED.value:
        seat.status = CrewStatus.PENDING.value
        seat.revoked_by = None
        seat.revoked_at = None
        seat.save(update_fields=["status", "revoked_by", "revoked_at", "updated_at"])
        reopened = seat
    _audit(action=FleetAction.CREW_REQUESTED, actor=user, vehicle=vehicle,
           crew=seat, sacco=vehicle.sacco, note="reopened" if reopened else "")
    return seat


@transaction.atomic
def approve_crew(seat: CrewMember, *, approver, now=None) -> CrewMember:
    """PENDING → APPROVED. The human gate, in order:
    1. approver is an admin OF THE SEAT'S sacco (cross-sacco → refused);
    2. approver is NOT the applicant (two-party: a different human says yes);
    3. the vehicle is ACTIVE (retired metal gains no authority).
    """
    if seat.status != CrewStatus.PENDING.value:
        raise ValidationError(_("Only a pending crew request can be approved."))
    if not is_sacco_admin(approver, seat.vehicle.sacco):
        raise ValidationError(
            _("Only a SACCO admin of this vehicle's SACCO can approve crew.")
        )
    if approver.pk == seat.user_id:
        raise ValidationError(
            _("Crew approval needs a second human — you cannot approve your own request.")
        )
    if seat.vehicle.status != VehicleStatus.ACTIVE.value:
        raise ValidationError(_("Retired vehicles cannot gain crew approvals."))
    seat.status = CrewStatus.APPROVED.value
    seat.approved_by = approver
    seat.approved_at = _now(now)
    seat.full_clean()
    seat.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    _audit(action=FleetAction.CREW_APPROVED, actor=approver,
           sacco=seat.vehicle.sacco, vehicle=seat.vehicle, crew=seat)
    return seat


@transaction.atomic
def revoke_crew(seat: CrewMember, *, revoker, now=None) -> CrewMember:
    """APPROVED → REVOKED; the operator closes the door they opened."""
    if seat.status != CrewStatus.APPROVED.value:
        raise ValidationError(_("Only approved crew can be revoked."))
    if not is_sacco_admin(revoker, seat.vehicle.sacco):
        raise ValidationError(
            _("Only a SACCO admin of this vehicle's SACCO can revoke crew.")
        )
    if revoker.pk == seat.user_id:
        raise ValidationError(
            _("Revocation is an operator action — ask the SACCO admin to remove you.")
        )
    seat.status = CrewStatus.REVOKED.value
    seat.revoked_by = revoker
    seat.revoked_at = _now(now)
    seat.full_clean()
    seat.save(update_fields=["status", "revoked_by", "revoked_at", "updated_at"])
    _audit(action=FleetAction.CREW_REVOKED, actor=revoker,
           sacco=seat.vehicle.sacco, vehicle=seat.vehicle, crew=seat)
    return seat


def pending_crew_qs(user) -> models.QuerySet[CrewMember]:
    """PENDING seats on vehicles `user` can act on (tenant-scoped door)."""
    return (
        CrewMember.objects.filter(status=CrewStatus.PENDING.value)
        .filter(vehicle__in=visible_vehicles(user))
        .select_related("vehicle__sacco", "vehicle", "user")
    )


def crew_seat_for(user, seat_pk) -> CrewMember | None:
    """Tenant-scoped single-seat fetch: None when the seat is outside the
    caller's saccos (caller turns None into 403 — never a blind update)."""
    return pending_or_approved_qs(user).filter(pk=seat_pk).first()


def pending_or_approved_qs(user) -> models.QuerySet[CrewMember]:
    return CrewMember.objects.filter(
        vehicle__in=visible_vehicles(user)
    ).select_related("vehicle__sacco", "vehicle", "user")


# ------------------------------------------------------------ crew authority
def approved_crew_seat(user) -> CrewMember | None:
    """The most recent approved seat — deterministic when several exist."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return (
        CrewMember.objects.filter(user=user, status=CrewStatus.APPROVED.value)
        .order_by("-approved_at", "-pk")
        .select_related("vehicle")
        .first()
    )


def crew_role_of(user) -> str | None:
    """'driver' | 'conductor' | None — transport-side shorthand (27/29)."""
    seat = approved_crew_seat(user)
    if seat is None:
        return None
    return (
        "driver"
        if seat.role == CrewRole.DRIVER.value
        else "conductor"
    )


def is_approved_crew(user) -> bool:
    return approved_crew_seat(user) is not None


def vehicles_of_crew(user) -> models.QuerySet[Vehicle]:
    """ACTIVE vehicles where `user` holds an APPROVED crew seat."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Vehicle.objects.none()
    return (
        Vehicle.objects.filter(
            crew__user=user,
            crew__status=CrewStatus.APPROVED.value,
            status=VehicleStatus.ACTIVE.value,
        )
        .distinct()
        .select_related("sacco")
        .order_by("plate")
    )


# ------------------------------------------------------------------ admins
@transaction.atomic
def grant_sacco_admin(*, user, sacco, granter, now=None) -> SaccoAdmin:
    """The bootstrap gate: platform STAFF plants the operator's admin seat.

    WHY staff-only, always: this seat grants approval power itself; letting
    operators mint operators (or letting a crew member self-promote) would
    put the human gate out of a human's hands. Idempotent: re-granting an
    existing pair returns the same row without an audit spam.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        raise ValidationError(_("An admin seat needs a member."))
    if sacco is None or not sacco.pk:
        raise ValidationError(_("An admin seat needs a SACCO."))
    if not is_moderator(granter):
        raise ValidationError(_("Only platform staff can grant SACCO adminship."))
    existing = SaccoAdmin.objects.filter(sacco=sacco, user=user).first()
    if existing:
        return existing
    row = SaccoAdmin(sacco=sacco, user=user, added_by=granter)
    row.full_clean()
    row.save()
    _audit(action=FleetAction.SACCO_ADMIN_GRANTED, actor=granter,
           sacco=sacco, note=getattr(user, "username", ""))
    return row


# -------------------------------------------------------------- dashboards
def operator_dashboard_context(user) -> dict:
    """Counts + vehicle summary the dashboard template renders."""
    vehicles = visible_vehicles(user)
    vehicles = vehicles.annotate(
        pending_crew=Count("crew", filter=Q(crew__status=CrewStatus.PENDING.value), distinct=True),
        approved_crew=Count("crew", filter=Q(crew__status=CrewStatus.APPROVED.value), distinct=True),
    )
    return {
        "saccos": list(saccos_of_admin(user)),
        "vehicles": vehicles,
        "pending_total": CrewMember.objects.filter(
            status=CrewStatus.PENDING.value, vehicle__in=visible_vehicles(user)
        ).count(),
    }
