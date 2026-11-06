"""models.py — the Nairobi route network (Prompt 25).

Models: Sacco, Route, Stage, StageAlias, RouteStage, Fare, CorrectionQueue.

WHY honesty is modelled, not promised: this app seeds from the 2018 Digital
Matatus GTFS, which is STALE. Every row therefore carries explicit quality
signals a template can show mechanically:

- ``Route.seed_origin`` / ``SEED_QUALITY_NOTE`` — "seed-quality (2018 GTFS)"
  label on anything the seeder created;
- ``Route.shape_source`` — GTFS shapes only when shapes.txt really had them;
  a route without a shape NEVER gets an invented line (pitfall in PROMPTS.md
  25): the view draws a stage-to-stage join and labels it as such;
- ``Stage.lat/lon`` are nullable — a stage without coordinates displays an
  honest "no coordinates" state, never a guessed point (KDPA: these are
  public stop locations, approximate is fine and labelled);
- ``Fare`` stores an OBSERVED RANGE (min/max) with a ``Confidence`` enum —
  fares vary in reality, so there is never a single "official" number.

KDPA 2019: Sacco.contact_info is free TEXT curated by staff (no phone-number
parsing, validation or public API exposure) — operators' business contact,
kept out of the public fleet surfaces (Prompt 26 consumes Sacco but not the
contact field).
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import Confidence, TimestampedModel


class RegistrationStatus(models.TextChoices):
    """SACCO legal standing with NTSA/county — staff-curated, not guessed."""

    UNREGISTERED = "UNREGISTERED", _("Unregistered")
    PENDING = "PENDING", _("Registration pending")
    REGISTERED = "REGISTERED", _("Registered")


class Route(models.Model):
    """A matatu route (e.g. Nairobi CBD ↔ Kinoo)."""

    SEED_QUALITY_NOTE = "seed-quality"  # constant label the UI pins on seed rows

    name_en = models.CharField(_("Name (English)"), max_length=120)
    # Kiswahili name is optional: many corridor names ARE Kiswahili already
    # ("Ambassadeur–Kinoo" is spelled the same in both) — blank allowed so we
    # never fabricate a translation.
    name_sw = models.CharField(_("Name (Kiswahili)"), max_length=120, blank=True, default="")
    # Corridor colour from GTFS route_color (hex "#RRGGBB"); blank when the
    # feed had none — the UI falls back to a neutral chip, never a fake colour.
    corridor_color = models.CharField(
        _("Corridor colour"), max_length=7, blank=True, default=""
    )
    saccos = models.ManyToManyField(
        "routes.Sacco", blank=True, related_name="routes", verbose_name=_("SACCOs")
    )
    # gtfs_id = raw route_id from the feed — the seeder's upsert key, so a
    # re-run can never duplicate rows. Nullable because admin-created routes
    # have no GTFS origin.
    gtfs_id = models.CharField(
        _("GTFS route_id"), max_length=64, null=True, blank=True, unique=True
    )
    # NOTE: True only for rows the 2018 zip seeded — drives the honest label.
    seed_origin = models.BooleanField(_("From GTFS seed"), default=False)
    # Shape discipline (Prompt 25 pitfall): SOURCE_GTFS only when shapes.txt
    # really contained this route's line; SOURCE_STAGES means the UI draws
    # stage-to-stage and SAYS so; SOURCE_NONE means no map line at all.
    class ShapeSource(models.TextChoices):
        GTFS = "GTFS", _("GTFS shapes.txt line")
        STAGE_TO_STAGE = "STAGE_TO_STAGE", _("Stage-to-stage join (approximate)")
        NONE = "NONE", _("No line available")

    shape_source = models.CharField(
        _("Shape source"), max_length=16, choices=ShapeSource.choices,
        default=ShapeSource.NONE,
    )
    # The real line ONLY: JSON [[lon, lat], ...] copied verbatim from
    # shapes.txt points (rounded, no interpolation/extension).
    shape_points = models.JSONField(
        _("GTFS shape points"), null=True, blank=True, default=None
    )

    class Meta:
        ordering = ["name_en"]
        constraints = [
            models.UniqueConstraint(
                fields=["name_en", "name_sw"], name="uniq_route_name"
            )
        ]

    def __str__(self) -> str:
        return self.name_en

    def save(self, *args, **kwargs):
        # GTFS hex colours arrive bare ("FFCB05"); normalise to "#FFCB05" so
        # the template can use the value directly in CSS. Never invent one.
        c = (self.corridor_color or "").strip()
        if c and not c.startswith("#"):
            self.corridor_color = f"#{c.upper()}"
        super().save(*args, **kwargs)

    @property
    def stage_order(self) -> models.QuerySet["RouteStage"]:
        """The ordered sequence — RouteStage is the ordered link table."""
        return self.stagelinks.select_related("stage").order_by("position")


class Sacco(models.Model):
    """A savings & credit co-operative operating matatus.

    contact_info: free text an admin/curator may fill ("office on Koinange
    St"). Deliberately NOT a phone field — no parsing, no validation, and it
    is never serialised into a public API response (KDPA hard rule); it is a
    staff-only curated display field.
    """

    name = models.CharField(_("SACCO name"), max_length=120, unique=True)
    contact_info = models.CharField(
        _("Contact info (staff-curated)"), max_length=240, blank=True, default=""
    )
    registration_status = models.CharField(
        _("Registration status"), max_length=16,
        choices=RegistrationStatus.choices, default=RegistrationStatus.UNREGISTERED.value,
        db_index=True,
    )
    gtfs_id = models.CharField(_("GTFS agency_id"), max_length=64, null=True, blank=True, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Stage(models.Model):
    """A stage/stop: matatu boarding and alighting point.

    lat/lon public stop coordinates (approximate is fine — labelled in the
    UI), nullable because some named stages genuinely have no coordinates yet;
    the honest state is an empty map badge, not a near-miss guess.
    """

    name = models.CharField(_("Stage name"), max_length=120)
    gtfs_id = models.CharField(_("GTFS stop_id"), max_length=64, null=True, blank=True, unique=True)
    lat = models.DecimalField(
        _("Latitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    lon = models.DecimalField(
        _("Longitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    # Continuous-stop awareness: True where crews accept hails/alights along
    # the segment near this stage, not only at the marked bay. Display-only
    # awareness flag — it grants no tracking authority (Prompts 26/27 own that).
    continuous_stop = models.BooleanField(_("Hailable en route"), default=False)
    seed_origin = models.BooleanField(_("From GTFS seed"), default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def coordinate(self) -> tuple[float, float] | None:
        """(lat, lon) or None — apps.core geo convention (human order)."""
        if self.lat is None or self.lon is None:
            return None
        return (float(self.lat), float(self.lon))


class StageAlias(models.Model):
    """Nickname ↔ stage lookup ("Ambassadeur" ↔ "Ambassador", "Manyanga").

    WHY a table, not a TextField on Stage: trigram search indexes one column
    per index; the migration adds a GIN pg_trgm index on `alias` so typo-ish
    nickname queries hit the index instead of a scan (same pattern the
    search app uses).
    """

    stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="aliases", verbose_name=_("Stage")
    )
    alias = models.CharField(_("Alias"), max_length=120, db_index=True)

    class Meta:
        ordering = ["alias"]
        constraints = [
            models.UniqueConstraint(fields=["stage", "alias"], name="uniq_stage_alias")
        ]

    def __str__(self) -> str:
        return f"{self.alias} → {self.stage.name}"


class RouteStage(models.Model):
    """Ordered membership of a Stage on a Route.

    WHY unique (route, position) only: some Nairobi routes loop and revisit a
    stage legitimately twice, so (route, stage) uniqueness would forbid real
    networks. Position is 1-based so template pagination maths stays boring.
    """

    route = models.ForeignKey(
        Route, on_delete=models.CASCADE, related_name="stagelinks", verbose_name=_("Route")
    )
    stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="routelinks", verbose_name=_("Stage")
    )
    position = models.PositiveIntegerField(_("Position"))

    class Meta:
        ordering = ["route", "position"]
        constraints = [
            models.UniqueConstraint(fields=["route", "position"], name="uniq_route_position")
        ]

    def __str__(self) -> str:
        return f"{self.route.name_en} @ {self.position}: {self.stage.name}"


class Fare(models.Model):
    """Observed fare range for one segment, with a Confidence source label.

    WHY ranges: matatu fares move with time of day, rain and fuel —
    PROMPTS.md hard rule is "store observed ranges, never a single official
    number". Both bounds are required; clean() rejects bad spans. GTFS feeds
    carry no fares, so rows come from admins/community — never auto-filled.
    """

    route = models.ForeignKey(
        Route, on_delete=models.CASCADE, related_name="fares", verbose_name=_("Route")
    )
    from_stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="fares_from", verbose_name=_("From stage")
    )
    to_stage = models.ForeignKey(
        Stage, on_delete=models.CASCADE, related_name="fares_to", verbose_name=_("To stage")
    )
    min_price = models.PositiveIntegerField(_("Observed minimum (KSh)"))
    max_price = models.PositiveIntegerField(_("Observed maximum (KSh)"))
    # Currency is fixed vocabulary: the pilot operates in Kenyan shillings.
    currency = models.CharField(_("Currency"), max_length=8, default="KES", editable=False)
    confidence = models.CharField(
        _("Source confidence"), max_length=16, choices=Confidence.choices,
        default=Confidence.COMMUNITY_REPORT.value,
    )
    observed_at = models.DateField(_("Observed on"), null=True, blank=True)
    note = models.CharField(_("Source note"), max_length=240, blank=True, default="")

    class Meta:
        ordering = ["route", "from_stage__name", "to_stage__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["route", "from_stage", "to_stage"], name="uniq_fare_segment"
            )
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.min_price is None or self.max_price is None:
            errors["max_price"] = _("Fares need BOTH observed bounds — never one official number.")
        elif self.max_price < self.min_price:
            errors["max_price"] = _("Observed maximum must be at least the observed minimum.")
        # WHY both stages distinct: a self-segment ("CBD → CBD") carries no
        # fare information and would only confuse the detail page.
        if self.from_stage_id and self.to_stage_id and self.from_stage_id == self.to_stage_id:
            errors["to_stage"] = _("From and to stages must differ.")
        if errors:
            raise ValidationError(errors)

    def range_display(self) -> str:
        """Machine-checked range string: "KSh 50–80" — always a span."""
        return f"KSh {self.min_price}–{self.max_price}"

    def confidence_display(self) -> str:
        return Confidence(self.confidence).label


class CorrectionKind(models.TextChoices):
    """What a community correction claims is wrong (Prompt 25 queue)."""

    WRONG_STAGE_POSITION = "WRONG_STAGE_POSITION", _("Wrong stage position")
    MISSING_ROUTE = "MISSING_ROUTE", _("Missing route")
    MISSING_STAGE = "MISSING_STAGE", _("Missing stage")
    WRONG_FARE = "WRONG_FARE", _("Wrong fare")


class CorrectionStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    RESOLVED = "RESOLVED", _("Resolved")


class CorrectionQueue(TimestampedModel):
    """Community corrections routed to moderators (LOCAL, on purpose).

    WHY a local model instead of moderation.Report: moderation's
    ALLOWED_TARGET_APPS allow-list does not include 'routes' yet — the parent
    wires the label at consolidation (see the NOTE in apps/routes/views.py).
    The local queue keeps the community flow working today and its fields
    mirror Report's (kind, note, opened_by, status, resolution_note) so a
    later migration can copy rows across unchanged.
    """

    kind = models.CharField(_("Correction kind"), max_length=24, choices=CorrectionKind.choices)
    route = models.ForeignKey(
        Route, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="corrections", verbose_name=_("Route"),
    )
    stage = models.ForeignKey(
        Stage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="corrections", verbose_name=_("Stage"),
    )
    note = models.TextField(_("What's wrong?"), max_length=500)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="route_corrections", verbose_name=_("Opened by"),
    )
    status = models.CharField(
        _("Status"), max_length=16, choices=CorrectionStatus.choices,
        default=CorrectionStatus.OPEN.value, db_index=True,
    )
    # Moderators fill this when resolving — the human gate (hard rule).
    resolution_note = models.CharField(_("Resolution note"), max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        # MISSING_* corrections may target no object; WRONG_* must point at
        # one, so the moderator can act without guessing.
        if self.kind in (CorrectionKind.WRONG_STAGE_POSITION, CorrectionKind.WRONG_FARE):
            if self.stage is None and self.route is None:
                raise ValidationError(_("Point the correction at a route or stage."))

    def __str__(self) -> str:
        return f"{self.kind} ({self.status}) by {self.opened_by}"
