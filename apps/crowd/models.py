"""models.py — passenger crowd reports (Prompt 30).

Models: ReportType, CrowdReport, CrowdReporterLink, ReportOutcome.

WHAT a crowd report IS: an EVENT AT A KNOWN STAGE, typed and time-stamped —
"3 riders report flooding at Kasarani since 09:20". It is NEVER a position:
no lat/lon, no "the matatu is at X" — that would be fabrication (PROMPTS.md
hard rule: no synthetic positions; missing data is said, not invented).
`Confidence.COMMUNITY_REPORT` is therefore a constant, not a column: a row
cannot belong to any other source class.

WHY a link table (CrowdReporterLink) instead of just a counter: the merge
must count DISTINCT RIDERS, not filings — the same person re-filing twice
within the window is one rider, not two. The link rows are the honest
ledger; ``merged_count`` is a cheap cache of them (kept equal in the same
transaction). Rider identities stay INTERNAL to this app — public surfaces
show "N riders report", never usernames (KDPA + task guardrail).

MERGE RULE (spec verbatim): a new report of the same (stage, kind) within
10 minutes merges into the still-open report: merged_count += 1 and
last_bumped_at = now. The surviving row keeps its FIRST reporter/note —
later riders have said the same thing, which is exactly what the count
expresses. Decay is computed AT READ TIME (weighting.py), so no stored
decay state can drift from the clock.

DECISION — NOT a moderation target: crowd reports are transport SIGNALS
(community evidence about road conditions), not user content surfaces for
moderation (Prompt 15 handles content violations via its allow-list; the
allow-list is deliberately UNTOUCHED here). Abuse is bounded structurally:
light anti-flood (services.MAX_REPORTS_PER_HOUR), trust weighting (a
low-trust reporter contributes little weight), and 2-hour expiry at read
time. A later prompt may add explicit stage-level resolution/expiry.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import Confidence, TimestampedModel

# Field sizes — shared with forms (one truth, no magic numbers in widgets).
MAX_NOTE_CHARS = 200  # plain, short: context for the signal, not a post
MAX_HINT_CHARS = 120  # "Ruiru Rd towards CBD" style free text

MSG_UNKNOWN_TYPE = _("Unknown report type — pick one of the listed kinds.")
MSG_HINT_NEEDS_SIGHTING = _(
    'A route/destination hint only fits a "matatu here now" sighting.'
)


class ReportType(models.TextChoices):
    """The report vocabulary — values persisted, NEVER labels (hard rule).

    MATATU_HERE is the POSITIVE sighting ("matatu here now"): it is evidence
    that a matatu IS present at the stage right now — still an event at the
    stage, never a guessed position; its optional destination hint is plain
    rider text, not a coordinate.
    """

    DELAY = "DELAY", _("Long delays at this stage")
    REROUTE = "REROUTE", _("Buses rerouted")
    STAGE_CLOSED = "STAGE_CLOSED", _("Stage closed")
    FLOODING = "FLOODING", _("Flooding")
    ACCIDENT = "ACCIDENT", _("Accident")
    MATATU_HERE = "MATATU_HERE", _("Matatu here now")


class ReportOutcome(models.TextChoices):
    """What file_report() did with the incoming signal (test vocabulary)."""

    CREATED = "CREATED", _("New report opened")
    MERGED = "MERGED", _("Merged into an open report")
    DUPLICATE = "DUPLICATE", _("Already counted this rider")


class CrowdReport(TimestampedModel):
    """One merged, honest transport signal at one stage.

    route is optional context (a stage can sit on several routes; the row is
    stage-primary — the FIRST reporter's route is kept so the display line
    can say "on Ruiru Rd"). stage is required: no stage, no event.
    """

    route = models.ForeignKey(
        "routes.Route", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crowd_reports", verbose_name=_("Route"),
    )
    stage = models.ForeignKey(
        "routes.Stage", on_delete=models.CASCADE,
        related_name="crowd_reports", verbose_name=_("Stage"),
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="crowd_reports", verbose_name=_("Reporter"),
    )
    kind = models.CharField(
        _("Report type"), max_length=16, choices=ReportType.choices, db_index=True,
    )
    note = models.CharField(
        _("Note (optional)"), max_length=MAX_NOTE_CHARS, blank=True, default=""
    )
    # MATATU_HERE ONLY ("heading Ruiru Rd"): clean() refuses it on other
    # kinds — a reroute has no honest "destination guess", only a sighting.
    destination_hint = models.CharField(
        _("Destination hint"), max_length=MAX_HINT_CHARS, blank=True, default=""
    )
    merged_count = models.PositiveIntegerField(_("Riders reported"), default=1)
    # The anchor of the merge window AND of read-time decay: when each
    # further rider joined. Null until the first merge — last_seen_at below
    # falls back to created_at so the pure weighting stays simple.
    last_bumped_at = models.DateTimeField(
        _("Last rider reported at"), null=True, blank=True, db_index=True
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["stage", "kind"])]

    def clean(self):
        super().clean()
        errors = {}
        if self.kind and self.kind not in ReportType.values:
            errors["kind"] = MSG_UNKNOWN_TYPE
        if self.destination_hint and self.kind != ReportType.MATATU_HERE.value:
            errors["destination_hint"] = MSG_HINT_NEEDS_SIGHTING
        if errors:
            raise ValidationError(errors)

    @property
    def confidence(self) -> str:
        """Crowd reports are COMMUNITY_REPORT by definition — a constant."""
        return Confidence.COMMUNITY_REPORT.value

    @property
    def last_seen_at(self):
        """The freshest rider evidence on this row (merge anchor)."""
        return self.last_bumped_at or self.created_at

    def display(self) -> str:
        """The honest line at read time ("3 riders report flooding...")."""
        from .display import rider_line

        return rider_line(
            self.kind, self.merged_count,
            stage_name=self.stage.name,
            route_name=self.route.name_en if self.route_id else None,
        )

    def __str__(self) -> str:
        return f"{self.kind} @ {self.stage.name} ×{self.merged_count}"


class CrowdReporterLink(TimestampedModel):
    """One rider's joining of one merged report (UNIQUE report+rider).

    WHY not just the counter: dedupe honesty. When the SAME reporter files
    the same (stage, kind) within the window the link already exists, so the
    merge is a no-op ("duplicate") and the count stays truthful. Riders'
    names are never rendered publicly anywhere in this app.
    """

    report = models.ForeignKey(
        CrowdReport, on_delete=models.CASCADE,
        related_name="rider_links", verbose_name=_("Report"),
    )
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="crowd_report_links", verbose_name=_("Rider"),
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["report", "rider"], name="crowd_uniq_report_rider"
            )
        ]

    def __str__(self) -> str:
        return f"rider {self.rider_id} → report {self.report_id}"
