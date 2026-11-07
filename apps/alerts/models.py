"""models.py — followed vehicles/routes + alert preferences + ledger (31).

Models: VehicleFollow, RouteFollow, FollowAlert (+ AlertFollowKind).

WHAT follows ARE here: matatu/route FOLLOW ROWS, not people-links. The
13 report's Follow model (apps.follows) targets USERS and stays untouched —
follows of metal live here. Asymmetric like the report: a rider subscribes
to a matatu or a corridor, approval-free.

NOTIFICATIONS ARE OPT-IN (spec; KDPA consent discipline): ``notify_enabled``
defaults False on every row. The ONE-TAP toggle flips it; until the rider
taps, alert_scan() sends NOTHING for that row — silence is the default
behaviour, not a bug.

The alert stage ("your matatu is coming" trigger) is PER FOLLOW ROW:
- RouteFollow picks one alert stage ON that route (validated in clean());
- VehicleFollow may pick one alert stage on ANY of the vehicle's routes.
  Without a chosen stage a follow still exists (it lists, it renders) but
  generates NO approach alerts — honest, no invented trigger.

APPROACH RADIUS is per follow row too (bounds 1–10 km, default 2 — the
spec's "configurable radius"; same bounds both kinds, one constant).

FollowAlert is the NOTIFY LEDGER and the dedupe mechanism: every send
records (follow, stage, session, notified_at) and a NEW send for the same
triple within ALERT_DEDUPE_MINUTES is suppressed by alert_service — ONE
notification per approach event, never a buzz-storm on every ping.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel

# ----------------------------------------------------------------- constants
# "configurable radius" (spec): default 2 km of the chosen stage; bounds so a
# typo can never widen an alert to half a county. Shared by both follow kinds.
DEFAULT_RADIUS_KM = 2
MIN_RADIUS_KM = 1
MAX_RADIUS_KM = 10

# Dedupe window (spec): a follow+stage+session approach is reported AT MOST
# once per 30 min — later pings inside the window are the SAME event.
ALERT_DEDUPE_MINUTES = 30

MSG_STAGE_NOT_ON_PATH = _(
    "Pick a stage that lies on this route."
)
MSG_STAGE_NOT_ON_VEHICLE = _(
    "Pick a stage that lies on one of this vehicle's routes."
)


class AlertFollowKind(models.TextChoices):
    """Which follow table a ledger row points at (values frozen vocabulary)."""

    VEHICLE = "VEHICLE", _("Vehicle follow")
    ROUTE = "ROUTE", _("Route follow")


class FollowBase(TimestampedModel):
    """Shared alert preferences of both follow kinds.

    Abstract on purpose: the FIELDS (opt-in flag, radius, chosen stage) are
    identical; the unique pair and stage/path validation differ per kind,
    and those belong to the concrete model where they are testable.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(class)ss",  # user.vehicle_follows / user.route_follows
        verbose_name=_("Rider"),
    )
    # DEFAULT OFF: notifications are opt-in, mutable in one tap (spec). No
    # consent flag, no notification — alert_scan never even looks at the row.
    notify_enabled = models.BooleanField(_("Notify me"), default=False)
    radius_km = models.PositiveSmallIntegerField(
        _("Approach radius (km)"),
        default=DEFAULT_RADIUS_KM,
        validators=[MinValueValidator(MIN_RADIUS_KM), MaxValueValidator(MAX_RADIUS_KM)],
        help_text=_(
            "Alert when the followed matatu's live ping comes within this many "
            "km of your alert stage (1–10 km)."
        ),
    )
    # The rider's chosen trigger stage. Nullable: a follow without a stage
    # simply raises no approach alerts (honest "you have not picked one yet").
    # SET_NULL: deleting an admin-removed stage must not delete people's
    # follows — it just clears the trigger until they pick another.
    notify_stage = models.ForeignKey(
        "routes.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",  # no reverse needed anywhere
        verbose_name=_("Alert stage"),
    )

    class Meta:
        abstract = True


class VehicleFollow(FollowBase):
    """A rider follows ONE matatu (resolved from a nickname to ACTIVE metal)."""

    vehicle = models.ForeignKey(
        "fleet.Vehicle",
        on_delete=models.CASCADE,  # the metal is gone → nothing to follow
        related_name="alert_followers",
        verbose_name=_("Vehicle"),
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "vehicle"], name="uniq_alert_vehicle_follow"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="alert_vfollow_user_idx"),
            # The scan filters enabled rows first — keep it cheap.
            models.Index(fields=["notify_enabled"], name="alert_vfollow_notify_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} follows {self.vehicle}"

    def clean(self):
        super().clean()
        # The chosen stage must lie on a route this vehicle operates — an
        # alert on a stage the matatu never serves would be a fabrication.
        stage = self.notify_stage
        if stage is not None and self.vehicle_id:
            on_path = stage.routelinks.filter(
                route__in=self.vehicle.routes.all()
            ).exists()
            if not on_path:
                raise ValidationError(MSG_STAGE_NOT_ON_VEHICLE)


class RouteFollow(FollowBase):
    """A rider follows ONE route (a corridor, not one matatu)."""

    route = models.ForeignKey(
        "routes.Route",
        on_delete=models.CASCADE,
        related_name="alert_followers",
        verbose_name=_("Route"),
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "route"], name="uniq_alert_route_follow"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="alert_rfollow_user_idx"),
            models.Index(fields=["notify_enabled"], name="alert_rfollow_notify_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} follows route {self.route}"

    def clean(self):
        super().clean()
        stage = self.notify_stage
        if stage is not None and self.route_id:
            on_path = self.route.stagelinks.filter(stage=stage).exists()
            if not on_path:
                raise ValidationError(MSG_STAGE_NOT_ON_PATH)


class FollowAlert(TimestampedModel):
    """One sent notification — the ledger AND the dedupe memory.

    WHY rows, not a cache/flag on the follow: the 30-min window is time
    arithmetic, and only a per-event record can answer "was this exact
    (follow, stage, session) event already announced at/after now-30min?"
    deterministically. Session FK: dedupe is per LIVE SESSION (a brand-new
    session is a brand-new event); CASCADE — the ledger has no meaning
    without its sources, and the window is short.
    """

    follow_kind = models.CharField(
        _("Follow kind"), max_length=8, choices=AlertFollowKind.choices, db_index=True
    )
    vehicle_follow = models.ForeignKey(
        "alerts.VehicleFollow",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alerts",
        verbose_name=_("Vehicle follow"),
    )
    route_follow = models.ForeignKey(
        "alerts.RouteFollow",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="alerts",
        verbose_name=_("Route follow"),
    )
    stage = models.ForeignKey(
        "routes.Stage",
        on_delete=models.CASCADE,
        related_name="alerts",
        verbose_name=_("Alert stage"),
    )
    session = models.ForeignKey(
        "tracking.TrackingSession",
        on_delete=models.CASCADE,
        related_name="follow_alerts",
        verbose_name=_("Tracking session"),
    )
    title = models.CharField(_("Alert title"), max_length=200)
    body = models.CharField(_("Alert body"), max_length=300)
    # Which channel answered (e.g. "mock:1") — what the adapter returned.
    adapter_id = models.CharField(_("Adapter receipt"), max_length=40, blank=True, default="")
    notified_at = models.DateTimeField(_("Notified at"), db_index=True)
    # NOT auto_now_add: the injected clock sets it (testability hard rule).

    class Meta:
        ordering = ["-notified_at"]
        indexes = [
            # The window-dedupe query filters (stage, session) then time.
            models.Index(
                fields=["stage", "session", "notified_at"], name="alert_dedupe_idx"
            ),
        ]
        constraints = [
            # Exactly ONE follow pointer per ledger row (check in DB, not just
            # in the service — a bad row must be unwriteable).
            models.CheckConstraint(
                condition=(
                    models.Q(vehicle_follow__isnull=False,
                             route_follow__isnull=True)
                    | (models.Q(vehicle_follow__isnull=True,
                                route_follow__isnull=False))
                ),
                name="alerts_one_follow_pointer",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.follow_kind} #{self.vehicle_follow_id or self.route_follow_id}"
            f" → {self.stage} @ {self.notified_at:%Y-%m-%d %H:%M}"
        )

    @property
    def follow(self):
        """The follow row this ledger entry is about (either kind)."""
        return self.vehicle_follow or self.route_follow
