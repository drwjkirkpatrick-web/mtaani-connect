"""Crowd services (Prompt 30) — one door for filing, merging, trust.

PUBLIC API for other modules (consume THESE, never internals):

    from apps.crowd import services

    services.file_report(reporter=..., stage=..., kind="DELAY",
                         route=None, note="", destination_hint="",
                         now=None)  # now injects the MERGE window clock
        -> (CrowdReport, ReportOutcome)
    services.open_reports_for_stage(stage, *, now)   # mergeable rows
    services.make_report_scorer()   # Prompt 16 trust -> 0..1 adapter
    services.recent_report_count(reporter, *, now)

DESIGN (documented):
- MERGE inside one transaction with the target row LOCKED
  (select_for_update): the count bump and its link row commit together or
  not at all, and concurrent riders can never stack a lost increment —
  the UNIQUE(report, rider) link stops double-counting inside the lock.
- MERGE keeps the OPEN report's note (first reporter's surviving row);
  later riders said the SAME thing — their contribution is the count,
  not an edited note.
- MERGE scope is (stage, kind) within MERGE_WINDOW_MIN, spec verbatim,
  measured from the row's last activity (last bump, else creation).
  Route differences do NOT split merges: a stage shared by two routes
  still gets ONE signal "at Kasarani"; the surviving row's route is
  display context only (decided — revisit at Prompt 32 if planner edges
  need per-route severing).
- NO decay state is stored anywhere: expiry happens at read time in
  weighting.py (pure, clock-injected), so a stale report can never
  re-freshen itself and tests replay history deterministically.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    CrowdReport,
    CrowdReporterLink,
    ReportOutcome,
)

# Spec verbatim: "same type+stage within 10 min merges with a count bump".
MERGE_WINDOW_MIN = 10
# Light anti-flood mirrored from routes' correction flow (10/hour/member):
# crowd truth must not be floodable from one phone.
MAX_REPORTS_PER_HOUR = 10


def _now(now):
    """The merge clock: injected `now` wins, otherwise the real clock."""
    return timezone.now() if now is None else now


def _open_filter(window_start) -> Q:
    """A row is OPEN when its last activity (a rider bump, or the row's
    own creation before any bump) sits within the merge window."""
    return Q(last_bumped_at__gte=window_start) | Q(
        last_bumped_at__isnull=True, created_at__gte=window_start
    )


def open_reports_for_stage(stage, *, now=None):
    """OPEN (still mergeable) reports at one stage, freshest activity
    first. Read-only, clock injected — file_report() applies the same
    predicate, so tests can replay merge windows deterministically."""
    window_start = _now(now) - timedelta(minutes=MERGE_WINDOW_MIN)
    return (
        CrowdReport.objects.filter(stage=stage)
        .filter(_open_filter(window_start))
        .order_by("-last_bumped_at", "-created_at")
    )


@transaction.atomic
def file_report(
    *,
    reporter,
    stage,
    kind,
    route=None,
    note="",
    destination_hint="",
    now=None,
):
    """File a rider signal — NEW row, or MERGE into an open one.

    Returns (report, ReportOutcome):
      CREATED   — fresh row; this reporter is its first rider;
      MERGED    — an open same-(stage, kind) row existed and a NEW rider
                  joined it: count bumped, never a second row;
      DUPLICATE — the same rider already backed that open row: nothing
                  changes (the count stays honest).

    `now` is the injectable merge clock (tests/replays); validation runs
    through the model's full_clean() so admin/forms/ORM inherit one gate.
    """
    if (
        reporter is None
        or not getattr(reporter, "is_authenticated", False)
        or reporter.pk is None
    ):
        raise ValidationError(_("Log in before filing a crowd report."))

    candidate = CrowdReport(
        reporter=reporter,
        stage=stage,
        route=route,
        kind=kind,
        note=note or "",
        destination_hint=destination_hint or "",
    )
    # ONE validation gate for every entry path: unknown kinds and
    # misplaced destination hints are refused BEFORE any merge scan.
    candidate.full_clean()

    now = _now(now)
    window_start = now - timedelta(minutes=MERGE_WINDOW_MIN)
    target = (
        CrowdReport.objects.select_for_update()
        .filter(stage_id=stage.pk, kind=candidate.kind)
        .filter(_open_filter(window_start))
        .order_by("-last_bumped_at", "-created_at")
        .first()
    )
    if target is not None:
        link, created_link = CrowdReporterLink.objects.get_or_create(
            report=target, rider=reporter
        )
        if created_link:
            # Row is locked above: a plain increment cannot lose an update
            # between the read and the save inside the same transaction.
            target.merged_count += 1
            target.last_bumped_at = now
            target.save(update_fields=["merged_count", "last_bumped_at", "updated_at"])
            return target, ReportOutcome.MERGED
        # Same rider again inside the window: the signal already counts
        # them — no bump, NO window extension (one person cannot keep a
        # signal fresher than independent riders can).
        return target, ReportOutcome.DUPLICATE

    candidate.save()
    CrowdReporterLink.objects.create(report=candidate, rider=reporter)
    return candidate, ReportOutcome.CREATED


def make_report_scorer():
    """Adapter: Prompt 16's PUBLIC usefulness_scorer as a trust_scorer.

    Mirrors feed's make_trust_scorer: a CLOSURE memoized per reporter so a
    page build costs one rollup lookup per DISTINCT reporter. Category is
    "GENERAL" — transport reports are everyday community knowledge, not a
    medical/electrical/construction speciality (the same string the feed
    passes to trust).
    """
    from apps.trust.services import usefulness_scorer as trust_scorer

    scores: dict[int, float] = {}

    def score(reporter) -> float:
        if reporter is None:
            return 0.0
        key = getattr(reporter, "pk", None)
        if key not in scores:
            scores[key] = trust_scorer(reporter, "GENERAL")
        return scores[key]

    return score


def recent_report_count(reporter, *, now=None) -> int:
    """COUNTED flood-actions the member made within the anti-flood hour.

    WHY links, not rows: after merging, several duplicates leave ONE row —
    counting CrowdReport rows would let a phone file 100 submissions that
    all no-op locally, then re-arm. Every CREATED or MERGED filing creates
    exactly ONE reporter-link row (duplicates create none), so links are
    the honest count of signals this member added in the window."""
    hour_start = _now(now) - timedelta(hours=1)
    return CrowdReporterLink.objects.filter(
        rider=reporter, created_at__gte=hour_start
    ).count()
