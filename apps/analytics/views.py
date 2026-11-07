"""Analytics views (Prompt 37) — staff dashboards, aggregate-only.

Rough role map (scoping tested in tests/test_analytics.py):
* analytics:dashboard — platform staff (staff_member_required); metric
  cards over a selectable 7/30-day window + the honest utility line
  (feed local share %; "no data yet" rather than a 0% lie).
* analytics:queue — moderators ONLY (moderation.services.is_moderator,
  their PUBLIC role door); queue age + report volume. No report listing
  here (no per-user surfaces) — the moderation queue itself stays theirs.
* analytics:sacco — SACCO operators ONLY, and only their own saccos
  (fleet.services PUBLIC doors enforce the tenant wall); another sacco's
  id in the URL is a 403.
* analytics:status — the dashboard's numbers as JSON (staff). No per-user
  list, no session replay, no pixels — anywhere, on purpose.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.analytics import aggregator
from apps.analytics.models import DailyAggregate, Metric

ALLOWED_WINDOW_DAYS = (7, 30)


def _days(request: HttpRequest) -> int:
    """Select the card window: 7 or 30, anything else honestly 7."""
    raw = request.GET.get("days")
    if raw and raw.isascii() and raw.isdigit():
        return int(raw) if int(raw) in ALLOWED_WINDOW_DAYS else 7
    return 7


def _cards(days: int, today) -> list[dict]:
    """One card per Metric: window number + today's number (None = no
    data observed — the template shows '—', never a fabricated 0)."""
    window = aggregator.window_metrics(days, today=today)
    today_window = aggregator.window_metrics(1, today=today)
    cards = []
    for metric in Metric:
        cards.append(
            {
                "key": metric.value,
                "label": metric.label,
                "window": window.get(metric.value),
                "today": today_window.get(metric.value),
            }
        )
    return cards


@staff_member_required
def dashboard(request: HttpRequest) -> HttpResponse:
    days = _days(request)
    today = timezone.localdate()
    feed_share = aggregator.window_metrics(days, today=today).get(
        Metric.FEED_LOCAL_SHARE_PCT.value
    )
    context = {
        "days": days,
        "days_choices": ALLOWED_WINDOW_DAYS,
        "cards": _cards(days, today),
        "feed_local_share_pct": feed_share,
        "range_start": today - timedelta(days=days - 1),
        "range_end": today,
    }
    return render(request, "analytics/dashboard.html", context)


@login_required
def queue(request: HttpRequest) -> HttpResponse:
    """Moderators only — and only aggregate queue facts (age, volume).

    NOTE: no report listing (individual rows with reporters) renders here —
    per-user surfaces stay in apps.moderation where they live today.
    login_required has already sent anonymous visitors to the login page;
    a logged-in non-moderator gets the 403.
    """
    from apps.moderation.services import is_moderator

    if not is_moderator(request.user):
        return HttpResponseForbidden(_("Moderators only."))
    today = timezone.localdate()
    volume = (
        DailyAggregate.objects.filter(
            metric=Metric.MODERATION_QUEUE_OPEN.value,
            date__gte=today - timedelta(days=6),
            date__lte=today,
        )
        .order_by("date")
    )
    context = {"status": aggregator.queue_status(), "volume_rows": volume, "days": 7}
    return render(request, "analytics/queue.html", context)


@login_required
def sacco(request: HttpRequest) -> HttpResponse:
    """A SACCO operator sees ONLY their own SACCO's fleet facts."""
    from apps.fleet.services import saccos_of_admin

    saccos = list(saccos_of_admin(request.user))
    if not saccos:
        # A non-operator (including platform staff) has no tenant wall to
        # stand inside — nothing was ever theirs to see.
        raise PermissionDenied(_("SACCO operator seat required."))
    raw = request.GET.get("sacco")
    selected = None
    if raw and raw.isascii() and raw.isdigit():
        selected = next((s for s in saccos if s.pk == int(raw)), None)
        if selected is None:
            # Cross-sacco probe: NOT theirs — refuse rather than silently
            # swap to a default (a swap would leak that other data exists).
            return HttpResponseForbidden(_("That SACCO is not yours."))
    if selected is None:
        selected = saccos[0]
    days = _days(request)
    hours = aggregator.sacco_session_hours(selected, days)
    context = {
        "saccos": saccos,
        "selected": selected,
        "days": days,
        "hours": hours,
    }
    return render(request, "analytics/sacco.html", context)


@staff_member_required
def status(request: HttpRequest) -> HttpResponse:
    """JSON mirror of the staff dashboard (same numbers, zero PII)."""
    days = _days(request)
    payload = {
        "range_days": days,
        "metrics": aggregator.window_metrics(days, today=timezone.localdate()),
        "queue": aggregator.queue_status(),
        "note": "Aggregate-only (prompt 37): counts by day, never by user.",
    }
    return JsonResponse(payload)
