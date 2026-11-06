"""models.py — vehicle registry, crew onboarding, SACCO adminship (Prompt 26).

Models: Vehicle, CrewMember, SaccoAdmin, FleetAudit (+ the small enums they
persist as ``.value``, never labels — PROMPTS.md hard rule).

WHAT this app is: the OPERATOR side of the transport flagship. A SACCO
(matatu co-operative) registers its vehicles and approves its crew. The
pitfall this file treats as a security contract:

    crew accounts must NOT inherit ordinary-user moderation blind spots —
    a crew role ADDS authority, it never removes accountability.

Concretely: approving a CrewMember grants position-publishing authority
(Consumed later by Prompt 27's tracking sessions via apps/routes/crew.py).
It grants nothing else — no moderation bypass, no report-duty relief, no
rate-limit relief. Those stay with apps.accounts / apps.moderation, and
tests/test_fleet.py pins the crew account's ordinary obligations.

KDPA 2019 (business data vs personal data): a plate and a nickname ("the
beloved matatu names") are business/public data — searchable by design.
Crew and vehicle rows NEVER carry personal contact info (phone/email are
accounts concerns; Sacco.contact_info is a staff-curated field this app
consumes-read-only and re-exposes nowhere).

Tenant isolation is not a template decoration: services.visible_vehicles()
is the SINGLE query-layer door operator code uses (views and the admin
helper); a SACCO A admin's queryset structurally contains no SACCO B rows.
"""
from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel

# --------------------------------------------------------------------- plates
# Kenyan plate families accepted (pragmatic, not NTSA-lawyer-complete):
#   modern   KCA 123K / MCX 123B        (3 letters, 3 digits, 1 letter)
#   newest   KDA 123AB                  (3 letters, 3 digits, 2 letters)
#   legacy   KA 100X                    (2-letter county era, trailing letter)
#   legacy   KZ 8463 / GKA 001          (no trailing letters at all)
# Separators optional (space/hyphen). Everything else — digits first, 2-digit
# groups, 4-letter prefixes, stray punctuation — is refused, not "fixed".
#
# WHY normalize at the storage layer: "kca 123k" and "KCA-123-K" are the
# same metal on the same matatu. Uppercase + hyphens makes the unique
# constraint case-insensitive-by-construction, so duplicate registration
# under another casing is caught by data, not by a lookup habit.
_PLATE_PATTERN = re.compile(r"^([A-Z]{2,3})-?(\d{3,4})-?([A-Z]{1,2})?$")
PLATE_MAX_LENGTH = 12  # KDA-123-AB == 11 chars — comfortable headroom

MSG_PLATE_REQUIRED = _(
    "A vehicle needs a registration plate (e.g. KCA 123K)."
)
MSG_PLATE_INVALID = _(
    "That is not a Kenyan plate we recognise. Examples: KCA 123K, "
    "KDA 123AB, KZ 8463 (legacy plates accepted too)."
)


def normalize_plate(raw_plate: str) -> str:
    """Uppercase, strip and hyphen-group a submitted plate; refuse garbage.

    Raises ValidationError (message above) for an empty or unrecognisable
    plate. Idempotent: feeding an already-normalized plate changes nothing.
    """
    candidate = (raw_plate or "").strip().upper().replace(" ", "-")
    match = _PLATE_PATTERN.fullmatch(candidate)
    if not match:
        if not (raw_plate or "").strip():
            raise ValidationError(MSG_PLATE_REQUIRED)
        raise ValidationError(MSG_PLATE_INVALID)
    prefix, digits, suffix = match.group(1), match.group(2), match.group(3)
    return f"{prefix}-{digits}" + (f"-{suffix}" if suffix else "")


class VehicleStatus(models.TextChoices):
    """Service status. RETIRED vehicles cannot gain crew approvals — a
    position-publishing authority parked in the yard is authority un-granted."""

    ACTIVE = "ACTIVE", _("Active")
    RETIRED = "RETIRED", _("Retired")


class Vehicle(TimestampedModel):
    """A registered matatu: plate, beloved nickname, capacity, SACCO owner.

    Plate is stored NORMALIZED (uppercase, hyphens); the raw-as-submitted
    form is ephemeral input, not data — a query for "KCA 123K" matches
    "KCA-123-K" because queries get normalized-or-icontains'd too (services).
    """

    plate = models.CharField(
        _("Registration plate"), max_length=PLATE_MAX_LENGTH, unique=True, db_index=True
    )
    nickname = models.CharField(
        _("Nickname"), max_length=60, blank=True, default="",
        help_text=_(
            "The beloved matatu names — searchable. Leave blank if it has none."
        ),
    )
    # Sanity bounds 5–80: 5 covers the crew van, 80 the longest city bus.
    # Typical Kenyan sizes in between: 14-seater vans, 33-seat buses,
    # 51-seat long-routes. A plain range beats inventing body-type rules.
    MIN_CAPACITY = 5
    MAX_CAPACITY = 80
    capacity = models.PositiveSmallIntegerField(
        _("Seats"),
        validators=[MinValueValidator(5), MaxValueValidator(80)],
        help_text=_("Seats (5–80)."),
    )
    sacco = models.ForeignKey(
        "routes.Sacco",
        on_delete=models.PROTECT,  # WHY: a SACCO row is an authority anchor —
        #  deleting it must not silently orphan vehicle authority; retire/
        #  delete vehicles first (staff action with its own audit trail).
        related_name="vehicles",
        verbose_name=_("SACCO"),
    )
    routes = models.ManyToManyField(
        "routes.Route", blank=True, related_name="vehicles", verbose_name=_("Routes")
    )
    status = models.CharField(
        _("Status"), max_length=12, choices=VehicleStatus.choices,
        default=VehicleStatus.ACTIVE.value, db_index=True,
    )

    class Meta:
        ordering = ["plate"]

    def __str__(self) -> str:
        return self.plate + (f" · {self.nickname}" if self.nickname else "")

    def clean(self):
        super().clean()
        errors = {}
        try:
            self.plate = normalize_plate(self.plate)
        except ValidationError as exc:
            errors["plate"] = exc.message
        # Case-insensitive duplicate surfaces as a FIELD error (normalization
        # makes different casings collide here, not only at the DB).
        if not errors and self.plate:
            duplicate = Vehicle.objects.filter(plate=self.plate).exclude(pk=self.pk)
            if duplicate.exists():
                errors["plate"] = _(
                    "A vehicle with this plate is already registered."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Defense in depth: even an ORM write that skipped full_clean() cannot
        # park a non-normalized (or outright garbage) plate in the database.
        if self.plate:
            self.plate = normalize_plate(self.plate)
        super().save(*args, **kwargs)

    def active_crew(self) -> models.QuerySet["CrewMember"]:
        """ONLY APPROVED crew — pending/revoked rows are history, not crew."""
        return self.crew.filter(status=CrewStatus.APPROVED.value)


class CrewRole(models.TextChoices):
    DRIVER = "DRIVER", _("Driver")
    CONDUCTOR = "CONDUCTOR", _("Conductor")


class CrewStatus(models.TextChoices):
    """Two-party human gate states: self-requested row sits PENDING until a
    DIFFERENT human (a SACCO admin) approves; REVOKED is an operator
    decision that a re-request can reopen (approval is never automatic)."""

    PENDING = "PENDING", _("Pending approval")
    APPROVED = "APPROVED", _("Approved")
    REVOKED = "REVOKED", _("Revoked")


class CrewMember(TimestampedModel):
    """A crew seat: one accounts user, one vehicle, one role, one gate.

    KDPA: the row stores the ROLE and APPROVAL facts — never contact info.
    ``approved_by``/``revoked_by`` point at the operator human who made the
    call, so "who said yes?" is answerable from data.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="fleet_crew", verbose_name=_("Member"),
    )
    vehicle = models.ForeignKey(
        "fleet.Vehicle", on_delete=models.CASCADE,
        related_name="crew", verbose_name=_("Vehicle"),
    )
    role = models.CharField(
        _("Crew role"), max_length=12, choices=CrewRole.choices, db_index=True
    )
    status = models.CharField(
        _("Approval state"), max_length=12, choices=CrewStatus.choices,
        default=CrewStatus.PENDING.value, db_index=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crew_approvals", verbose_name=_("Approved by"),
    )
    approved_at = models.DateTimeField(_("Approved at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crew_revocations", verbose_name=_("Revoked by"),
    )
    revoked_at = models.DateTimeField(_("Revoked at"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One row per (member, vehicle, role): the state machine lives on
            # that row (PENDING → APPROVED → REVOKED → …) instead of accreting
            # look-alike rows whose "current" state needs guessing.
            models.UniqueConstraint(
                fields=["user", "vehicle", "role"], name="uniq_fleet_crew_seat"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} — {self.role} on {self.vehicle} ({self.status})"

    def clean(self):
        super().clean()
        errors = {}
        if self.status == CrewStatus.PENDING.value and (
            self.approved_by_id or self.approved_at or self.revoked_by_id or self.revoked_at
        ):
            errors["status"] = _("A pending request has no approval or revocation yet.")
        if self.status == CrewStatus.APPROVED.value and not (
            self.approved_by_id and self.approved_at
        ):
            errors["status"] = _("An approval needs the approving human and moment.")
        if self.status == CrewStatus.REVOKED.value and not (
            self.revoked_by_id and self.revoked_at
        ):
            errors["status"] = _("A revocation needs the revoking human and moment.")
        if errors:
            raise ValidationError(errors)


class SaccoAdmin(TimestampedModel):
    """Operator gatekeeper: a user who may manage ONE SACCO's fleet.

    Bootstrap (the human gate at the bottom): the first admin of a SACCO is
    granted by platform staff only (services.grant_sacco_admin), because
    "operator says yes" needs an operator to exist — and nobody can
    self-promote into that seat. Further admins are still staff-granted
    (the seat is authority-over-authority; keep it boring and traceable).
    """

    sacco = models.ForeignKey(
        "routes.Sacco", on_delete=models.CASCADE,
        related_name="admins", verbose_name=_("SACCO"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="sacco_adminships", verbose_name=_("Admin"),
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="sacco_admin_grants", verbose_name=_("Granted by"),
    )

    class Meta:
        ordering = ["sacco__name", "user__username"]
        constraints = [
            models.UniqueConstraint(fields=["sacco", "user"], name="uniq_fleet_sacco_admin")
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.sacco}"


class FleetAction(models.TextChoices):
    """Local audit vocabulary — WHY not moderation.ModeratorAudit: its
    AuditAction enum is moderation's frozen vocabulary and its generic
    target allow-list does not include fleet objects; stretching a security
    log across apps couples both. Same reasoning as routes' local
    CorrectionQueue: rows stay local, layouts stay mirrorable."""

    VEHICLE_REGISTERED = "VEHICLE_REGISTERED", _("Vehicle registered")
    VEHICLE_UPDATED = "VEHICLE_UPDATED", _("Vehicle updated")
    VEHICLE_RETIRED = "VEHICLE_RETIRED", _("Vehicle retired")
    CREW_REQUESTED = "CREW_REQUESTED", _("Crew seat requested")
    CREW_APPROVED = "CREW_APPROVED", _("Crew seat approved")
    CREW_REVOKED = "CREW_REVOKED", _("Crew seat revoked")
    SACCO_ADMIN_GRANTED = "SACCO_ADMIN_GRANTED", _("SACCO admin granted")


class FleetAudit(TimestampedModel):
    """Append-only operator trail: every authority change leaves a row.

    References goNullable-SET_NULL so history survives even a later vehicle
    or crew deletion (you can always answer "who approved whom, when?").
    """

    action = models.CharField(_("Action"), max_length=24, choices=FleetAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fleet_audit_rows", verbose_name=_("Actor"),
    )
    sacco = models.ForeignKey(
        "routes.Sacco", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fleet_audits", verbose_name=_("SACCO"),
    )
    vehicle = models.ForeignKey(
        "fleet.Vehicle", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fleet_audits", verbose_name=_("Vehicle"),
    )
    crew = models.ForeignKey(
        "fleet.CrewMember", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fleet_audits", verbose_name=_("Crew seat"),
    )
    note = models.CharField(_("Note"), max_length=240, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} by {self.actor} ({self.created_at:%Y-%m-%d})"
