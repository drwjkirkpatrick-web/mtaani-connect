"""models.py — TrackingSession + PingPoint: the privacy core (Prompt 27).

WHAT a TrackingSession is: (vehicle, authorized by SACCO/crew, starts, ends,
token). It is the ENTIRE authorization surface for live positions — brief §5
non-negotiable: no live authorized session means NO publishable position,
ever, anywhere in the product.

DPIA NOTE (KDPA 2019 — high-risk processing): continuous precise vehicle
location is high-risk personal-linked processing. This app IS the planned
mitigation, enforced in code (apps/tracking/services.py), not in policy PDFs:
  (1) positions are queryable ONLY while the session is live;
  (2) the session auto-expires at its cap (default 12 h, configurable
      shorter — a crew who forgets to press stop stops publishing);
  (3) ping rows are pruned 72 h after capture;
  (4) boundary suppression in the query layer makes the public map unable to
      reconstruct driver off-trip movement around session boundaries (the
      depot/home reveal on the same trip);
  (5) tokens are scoped, rotated, hashed at rest and never expose crew
      identity.
Any future consumer (views, Prompt 28's map, later APIs) inherits all of it
because the rules live in the QUERY LAYER, not in templates (the pitfall).

NO stored status column: LIVE/ENDED/EXPIRED is DERIVED at read time from
ends_at + now (services.session_status). WHY: a stored status could drift
from ``ends_at``/cap_hours — the two fields that actually decide visibility —
and drift on THIS model means leaking positions after authorization died.
The sweep stamps ``ends_at``; the derived read is the contract.

TOKEN AT REST: ``token_hash`` only — sha256 over the raw token BOUND to the
(session id, vehicle id) scope (services.token_digest). The raw token exists
once, on the display-once page at creation/rotation; it is never stored,
logged or re-displayed.

KDPA split (same as fleet): plate + nickname are business data; crew
IDENTITY (who drives) is not serialized publicly — authorized_by is the
authority audit record, never a public field.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import Confidence, TimestampedModel

# ----------------------------------------------------------------- constants
# The DEFAULT cap (module constant per spec): a crew member who forgets to
# stop publishing is cut off automatically at 12 h. Per-session caps may be
# SHORTER (a short city loop needs no 12 h window) — never longer.
DEFAULT_CAP_HOURS = 12

# Ping retention: positions vanish 72 h after capture (privacy + storage
# minimization together). The retention JOB is services.prune_pings; the
# visibility read excludes over-age pings independently of whether the job
# has run (defense in depth — see services.visible_positions).
PING_RETENTION_HOURS = 72

# The 1 km privacy radius: the boundary-suppression ring. Any ping inside
# this distance of a session boundary anchor is suppressed on every read.
PRIVACY_RADIUS_KM = 1.0
PRIVACY_RADIUS_METERS = PRIVACY_RADIUS_KM * 1000.0


class AuthorizationSource(models.TextChoices):
    """Which operator door said yes (recorded on the row, who/which door)."""

    CREW = "CREW", _("Approved crew (self)")
    SACCO_ADMIN = "SACCO_ADMIN", _("SACCO admin")


class SessionStatus(models.TextChoices):
    """Derived session states — NEVER a stored column (module docstring).

    EXPIRED deliberately covers both the swept row (ends_at stamped,
    ended_reason EXPIRED) and the unswept deadline casualty: both are equally
    dead to consumers, and session_status() reports the same truth because
    the read derives from the deadline, not from whether a cron ran.
    """

    LIVE = "LIVE", _("Live")
    ENDED = "ENDED", _("Ended")
    EXPIRED = "EXPIRED", _("Expired")


class SessionEndedReason(models.TextChoices):
    """WHY a session closed — human door (STOPPED) vs automatic (EXPIRED)."""

    STOPPED = "STOPPED", _("Stopped by crew or SACCO admin")
    EXPIRED = "EXPIRED", _("Reached the session cap (auto-expired)")


class TrackingSession(TimestampedModel):
    """One authorized live-tracking window for one vehicle.

    authorized_by + authorization_source are the authority AUDIT facts
    ("who said yes, through which door") — they are never serialized into a
    public payload (KDPA: crew identity absence is tested in
    tests/test_tracking_sessions.py). cap_hours is the granted window: the
    service refuses longer-than-default grants, so 12 h stays the global
    ceiling even if a caller lies about it.
    """

    vehicle = models.ForeignKey(
        "fleet.Vehicle",
        on_delete=models.PROTECT,  # WHY PROTECT: a session is an authority
        # audit record — deleting a vehicle must not quietly erase it.
        related_name="tracking_sessions",
        verbose_name=_("Vehicle"),
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="tracking_sessions_authorized", verbose_name=_("Authorized by"),
    )
    authorization_source = models.CharField(
        _("Authorization source"), max_length=12, choices=AuthorizationSource.choices,
        db_index=True,
    )
    # AT REST ONLY the sha256 hex digest of the scope-baked raw token. WHY
    # 64 chars: hexdigest length. Unique so a leaked token maps to exactly
    # one session even at DB level.
    token_hash = models.CharField(
        _("Token hash"), max_length=64, unique=True, editable=False,
    )
    started_at = models.DateTimeField(
        _("Started at"),  # explicit (injected clock), NOT auto_now_add —
        # the START moment is the operator/crew's declared moment and every
        # cap deadline is measured from it deterministically.
        db_index=True,
    )
    # None while live. The single most consequential NULL in the app:
    # visibility gate rule (1) is literally "ends_at is null and now < cap".
    ends_at = models.DateTimeField(
        _("Ended at"), null=True, blank=True, db_index=True,
    )
    cap_hours = models.PositiveSmallIntegerField(
        _("Session cap (hours)"),  # 1..DEFAULT_CAP_HOURS, model.clean + service
    )
    ended_reason = models.CharField(
        _("Ended reason"), max_length=12, choices=SessionEndedReason.choices,
        blank=True, default="", db_index=True,
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            # The live-session scan (rule 1 read + both sweeps) filters
            # ends_at first — index it with the vehicle for tenant reads.
            models.Index(fields=["vehicle", "ends_at"], name="track_sess_veh_ends_idx"),
            models.Index(fields=["ends_at"], name="track_sess_ends_idx"),
        ]
        constraints = [
            # DB FLOOR for "one LIVE session per vehicle": at most one row
            # per vehicle with ends_at NULL. The service refuses a second
            # live start; this constraint is the race-condition backstop.
            # WHY workable with late stamps: start_session first stamps any
            # overdue-unswept row of THIS vehicle as EXPIRED, so NULL really
            # means "publishing right now".
            models.UniqueConstraint(
                fields=["vehicle"], condition=models.Q(ends_at__isnull=True),
                name="uniq_tracking_one_live_per_vehicle",
            ),
        ]

    def __str__(self) -> str:
        state = "live" if self.ends_at is None else self.ends_at.strftime("%Y-%m-%d %H:%M")
        return f"tracking {self.vehicle} · {state}"

    def deadline(self) -> datetime:
        """The hard expiry moment: started_at + cap_hours (the injected
        clock's contract — every derived read measures against THIS)."""
        return self.started_at + timedelta(hours=self.cap_hours)

    def clean(self):
        super().clean()
        errors = {}
        # Cap bounds: a grant may be shorter than the default, never longer
        # (the brief's cap is a ceiling, and a longer "shorter session" is
        # exactly the leak this guardrail exists to deny).
        if not (1 <= self.cap_hours <= DEFAULT_CAP_HOURS):
            errors["cap_hours"] = _(
                "Session cap must be between 1 and %(max)s hours."
            ) % {"max": DEFAULT_CAP_HOURS}
        if self.ends_at is not None:
            # ends_at must sit inside the granted window — an end moment
            # past the deadline would be a permission extension through the
            # "stop" button; started-relative ends_at is a clock artifact.
            if self.ends_at < self.started_at:
                errors["ends_at"] = _("A session cannot end before it starts.")
            elif self.ends_at > self.deadline():
                errors["ends_at"] = _(
                    "A session cannot end past its cap — it already has."
                )
            if not self.ended_reason:
                errors["ended_reason"] = _("An ended session records why.")
        elif self.ended_reason:
            errors["ended_reason"] = _("A live session has no ended reason.")
        if self.authorization_source not in AuthorizationSource.values:
            if not self.authorization_source:
                errors["authorization_source"] = _("Record who authorized it.")
            else:
                errors["authorization_source"] = _("Unknown authorization source.")
        if self.token_hash and len(self.token_hash) != 64:
            # Stored token is ALWAYS the 64-char sha256 hexdigest — the raw
            # token is display-once and never persisted (module docstring).
            errors["token_hash"] = _("Token hash must be a sha256 hex digest.")
        if errors:
            raise ValidationError(errors)


class PingPoint(TimestampedModel):
    """One reported vehicle position while the session was authorized.

    Plain Decimal lat/lon columns + SQL-cast geography (ADR-0001 amendment —
    no GeoDjango fields); the suppression ring casts on the fly in
    services.visible_positions. (session, recorded_at) is UNIQUE: dedupe is
    28's ingest responsibility, but a model-level unique is the floor so a
    buggy caller cannot flood duplicate fixes past the sweeper.
    """

    session = models.ForeignKey(
        TrackingSession,
        on_delete=models.PROTECT,  # WHY PROTECT: pings are the position
        # history; nothing deletes a session under their feet.
        related_name="pings",
        verbose_name=_("Session"),
    )
    # 9 digits total: lon "180.000000" fits exactly (3 + 6); lat comfortably.
    lat = models.DecimalField(_("Latitude"), max_digits=9, decimal_places=6)
    lon = models.DecimalField(_("Longitude"), max_digits=9, decimal_places=6)
    # --- Prompt 28 additive columns ONLY (no existing field changed) --------
    # WHY a per-ping speed column: the ingest plausibility review (ingest.py,
    # IMPOSSIBLE_SPEED_KMH) needs the recorded speed to JUDGE and to DISPLAY.
    # Null when the device sent none. Device-reported, or the server-computed
    # implied speed when IT triggered the review (ingest.py documents where).
    speed_kmh = models.DecimalField(
        _("Speed (km/h)"), max_digits=6, decimal_places=2, null=True, blank=True,
        help_text=_("Device-reported speed; fill from implied speed when the "
                    "plausibility review downgraded the ping."),
    )
    # Source label AT THE PING LAYER (Confidence enum): LIVE_GPS for a normal
    # authorized fix, ESTIMATE when the plausibility review kept the row but
    # flagged it — the map must render the label HONESTLY (never silently
    # upgrade it). Default LIVE_GPS keeps the pre-28 semantic for existing
    # rows: session-authorized fixes were live GPS by definition.
    confidence = models.CharField(
        _("Ping confidence"),
        max_length=16, choices=Confidence.choices,
        default=Confidence.LIVE_GPS.value, db_index=True,
    )
    recorded_at = models.DateTimeField(
        _("Recorded at"),  # device stamp — the dedupe key (28's ingest)
        help_text=_("Device clock at capture; dedupe key per session."),
    )
    received_at = models.DateTimeField(
        _("Received at"),  # server arrival — never equals recorded_at on 3G
        help_text=_("Server arrival moment (mock/real ingest set it together)."),
    )

    class Meta:
        ordering = ["recorded_at", "pk"]
        indexes = [
            # prune_pings + the 72 h visibility cut scan recorded_at.
            models.Index(fields=["recorded_at"], name="track_ping_recorded_idx"),
        ]
        constraints = [
            # The MODEL-LEVEL dedupe floor: one ping per (session, device
            # moment). 28's batched ingest dedupes BEFORE insert; this
            # constraint is what stands even if nobody remembers to.
            models.UniqueConstraint(
                fields=["session", "recorded_at"], name="uniq_tracking_ping_recorded"
            ),
        ]

    def __str__(self) -> str:
        return f"ping {self.session_id} @ {self.recorded_at:%Y-%m-%d %H:%M:%S}Z"
