"""Views for the crowd app (Prompt 30).

Public reads (weighted, honest): the per-stage report feed.
Writes (login): filing a report — merges/dedupes handled by services.
Every surfaced signal says what it is: "N riders report ... at <stage>" —
no reporter identities, no positions.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.routes.models import Stage

from . import serialization
from . import services
from .forms import ReportForm
from .models import CrowdReport

PAGE_SIZE = 20  # bounded lists: no bottomless pit (PROMPTS.md hard rule)


def index(request):
    """/crowd/ → the member's own reports (login gate handles anonymous)."""
    return redirect(reverse("crowd:mine"))


def stage_reports(request, pk: int):
    """Current, weighted crowd reports for ONE stage — the honest feed.

    Uses the SAME serializers.build as the Prompt 28 map overlay (scorer
    injected): the map and this page cannot drift apart.
    """
    stage = get_object_or_404(Stage, pk=pk)
    scorer = services.make_report_scorer()
    rows = serialization.crowd_overlay_for_stage(
        stage, now=timezone.now(), scorer=scorer
    )
    context = {
        "stage": stage,
        "rows": rows,
        "report_url": reverse("crowd:report_new") + f"?stage={stage.pk}",
    }
    return render(request, "crowd/stage_reports.html", context)


@login_required
def report_new(request):
    """File a crowd report (ANY authenticated member may report).

    The service merges/dedupes; the anti-flood limit bounds one phone's
    ability to shout. Unknown types / misplaced hints arrive as form
    errors from the same model.full_clean() gate the ORM uses.
    """
    if request.method == "POST":
        recent = services.recent_report_count(request.user, now=timezone.now())
        if recent >= services.MAX_REPORTS_PER_HOUR:
            messages.error(
                request,
                _("You have reached the hourly limit of %(limit)s reports.")
                % {"limit": services.MAX_REPORTS_PER_HOUR},
            )
            return redirect("crowd:mine")
        form = ReportForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            try:
                report, outcome = services.file_report(
                    reporter=request.user,
                    stage=data["stage"],
                    route=data["route"],
                    kind=data["kind"],
                    note=data["note"],
                    destination_hint=data["destination_hint"],
                    now=timezone.now(),  # injected even here: one clock pattern
                )
            except ValidationError as exc:
                if exc.error_dict:
                    for field, errs in exc.error_dict.items():
                        form.add_error(field, errs)
                else:
                    form.add_error(None, exc.messages)
            else:
                if outcome.value == "MERGED":
                    messages.success(
                        request,
                        _(
                            "Thank you — your report was merged with the other "
                            "riders reporting the same thing at this stage."
                        ),
                    )
                else:
                    messages.success(
                        request,
                        _("Thank you — your report is live for other riders."),
                    )
                return redirect("crowd:stage", pk=report.stage_id)
    else:
        initial = {}
        for key, field_name in (("stage", "stage"), ("route", "route")):
            value = request.GET.get(key)
            if value:
                initial[field_name] = value
        form = ReportForm(initial=initial)
    return render(request, "crowd/report_form.html", {"form": form})


@login_required
def mine(request):
    """The member's own filings — history view, oldest of the noise."""
    rows = CrowdReport.objects.filter(reporter=request.user).select_related(
        "stage", "route"
    )[:PAGE_SIZE]
    return render(request, "crowd/my_reports.html", {"rows": rows})
