"""Views for the AI app (PROMPTS.md Prompt 33) — staff-only status + probe.

Everything here is admin-ish (``staff_member_required``): the ledger
tracks spend and config health; it is not a member-facing feature and
NOTHING here publishes content.

The status PAGE and the JSON mirror share ``_ai_status()`` — one truth,
no drift (the alerts arrivals page+JSON pattern).

THE PROBE proves the door end to end: one POST → exactly ONE run_call
(= one ledger row) using the SHIPPED probe prompt file, whose labelled
draft result renders on the spot ("AI-assisted" badge). It never writes
content anywhere — the no_auto_publish test pins the ledger as the only
row source.

HONEST DEGRADE (never a 500): a spent budget or a stub provider left in
settings raises ledger.AiUnavailable; every view renders it as the
exception's honest "AI temporarily unavailable" message. A ValueError
from an unknown settings name (misconfig) degrades the same way.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import ledger, prompts, providers
from .labeling import ai_label

#: Bounded lists (hard rule): the ledger stays readable at any volume.
MAX_RECENT_CALLS = 20
#: The JSON mirror is for dashboards — smaller bounded window still.
MAX_JSON_CALLS = 20

UNAVAILABLE_BUDGET_MSG = _(
    "AI temporarily unavailable — the daily token budget is spent."
)


def index(request: HttpRequest) -> HttpResponse:
    """/ai/ → the status page (the only real surface of this app)."""
    return redirect(reverse("ai:status_page"))


def _ai_status() -> dict:
    """Shared truth for the page and the JSON mirror (never drift).

    Returns a plain dict of frozen numbers + the resolved provider state
    plus the honest ``unavailable_message`` when the layer cannot serve
    (budget spent / unknown provider name). Provider resolution errors
    degrade to a message — the status page's ONE job is to be truthful
    about the layer, never to crash on its own misconfiguration.
    """
    used, remaining = ledger.daily_budget_guard()
    configured = getattr(
        settings, "AI_PROVIDER_NAME", providers.DEFAULT_PROVIDER
    )
    item = {
        "used_tokens": used,
        "budget": ledger.AI_DAILY_TOKEN_BUDGET,
        "remaining_tokens": remaining,
        "provider_setting": configured,
        # None until proven usable — dashboards key off this safely.
        "provider_active": None,
        "unavailable": remaining <= 0,
        "unavailable_message": "",
    }
    if remaining <= 0:
        item["unavailable_message"] = str(UNAVAILABLE_BUDGET_MSG)
        return item
    try:
        providers.get_provider(configured)
    except ValueError as exc:
        item["unavailable"] = True
        item["unavailable_message"] = str(exc)
        return item
    item["provider_active"] = providers.get_provider(configured).provider_id
    return item


def _status_context(**extra) -> dict:
    """The full render context: numbers + recent ledger rows (bounded)."""
    context = {
        "status": _ai_status(),
        "recent_calls": ledger.AICall.objects.all()[:MAX_RECENT_CALLS],
        "probe": None,
        "probe_label": "",
        "probe_class": "",
        "probe_error": "",
        "probe_row": None,
    }
    context.update(extra)
    return context


@staff_member_required
def status_page(request: HttpRequest) -> HttpResponse:
    """The admin-ish GET page: today's spend, remaining, recent ledger."""
    return render(request, "ai/status.html", _status_context())


@staff_member_required
@require_GET
def status_json(request: HttpRequest) -> HttpResponse:
    """The dashboard's ``ai:status`` JSON mirror of the page (same truth)."""
    item = dict(_ai_status())
    item["label"] = {"text": ai_label()[0], "class": ai_label()[1]}
    item["recent_calls"] = [
        {
            "provider": row.provider,
            "model": row.model,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
            "total_tokens": row.total_tokens,
            "elapsed_ms": row.elapsed_ms,
            "purpose": row.purpose,
            "created_at": row.created_at.isoformat(),
        }
        for row in ledger.AICall.objects.all()[:MAX_JSON_CALLS]
    ]
    return JsonResponse(item)


@staff_member_required
@require_POST
def mock_probe(request: HttpRequest) -> HttpResponse:
    """The 'mock echo' probe button: ONE Mock-provider call, shown labelled.

    WHY it exists: a one-tap end-to-end proof (prompt file → provider →
    ledger row → labelled draft on screen) that touches nothing else —
    the draft is shown to the clicking staff member and published
    nowhere. Degrades honestly: budget spent or a stub provider renders
    the AiUnavailable message instead of a result — and logs NO row.
    """
    context = _status_context()
    try:
        result, row = ledger.run_call(
            prompts.prompt_text("probe-echo-v1"),
            [{"role": "user", "content": "status-page echo probe"}],
            max_tokens=60,
            purpose="status-probe",
        )
    except ledger.AiUnavailable as exc:
        context["probe_error"] = str(exc.message)
    else:
        context["probe"] = result
        context["probe_row"] = row
        context["probe_label"], context["probe_class"] = ai_label()
    return render(request, "ai/status.html", context)
