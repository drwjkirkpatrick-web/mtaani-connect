"""Views for the AI app: Prompt 33 staff status/probe + Prompt 34 "Ask
Mtaani" member assistant.

The Prompt 33 half is admin-ish (``staff_member_required``): the ledger
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
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from . import assistant, ledger, prompts, providers
from .forms import AskAssistantForm
from .labeling import ai_label
from .models import AssistantRun

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


@login_required
def ask(request: HttpRequest) -> HttpResponse:
    """'Ask Mtaani' (Prompt 34) — the member-facing local-adaptation
    assistant. GET: the guided-intake form (what do you have / what do you
    need) + the honest per-user today-meter. POST: the full door —
    quota → guides corpus retrieval → ONE ledgered run_call with the
    STRICT versioned prompt (assistant-v2) → the safety gate (gated
    categories append the cited guides' own warnings VERBATIM) → one
    AssistantRun row → the draft render: AI badge (Prompt 12), cited
    guides (titled + linked), safety block, and the community-verify CTA.

    DEGRADES HONESTLY (never a 500, never a fake answer):
    - assistant.AssistantQuotaExceeded → "AI assistance is resting for
      today…" — the per-user free-tier quota (brief §8); NO AI content in
      this state, so NO AI badge (the label covers AI content; none shows);
    - ledger.AiUnavailable → "AI temporarily unavailable…" (budget spent
      or a stub/misconfigured provider) — same honesty, no badge, no row.

    WHY login_required: the quota is PER USER and the verify click writes
    the member's run row; this is the app's first member surface (the
    ledger bookkeeping below stays staff-only).
    """
    form = AskAssistantForm(request.POST or None)
    used = assistant.runs_used_today(request.user)
    context = {
        "form": form,
        "runs_used": used,
        "runs_limit": assistant.ASSISTANT_DAILY_RUNS_PER_USER,
        "runs_remaining": assistant.quota_remaining(request.user),
        "result": None,
        "state_message": "",
        "verify_thanks": False,
        "label": ai_label(),  # the render's badge pair (Prompt 12)
    }
    if request.method == "POST" and form.is_valid():
        try:
            context["result"] = assistant.ask(
                request.user,
                have_text=form.cleaned_data["have_text"],
                need_text=form.cleaned_data["need_text"],
            )
        except assistant.AssistantQuotaExceeded as exc:
            context["state_message"] = str(exc.message)
        except ledger.AiUnavailable as exc:
            context["state_message"] = str(exc.message)
    return render(request, "ai/ask.html", context)


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


@login_required
@require_POST
def ask_helpful(request: HttpRequest, pk: int) -> HttpResponse:
    """The community-verification click (Prompt 34): "Did this work for
    you?" → ONE honest boolean on the member's OWN run row + a plain
    thanks message, rendered without any further processing.

    BOUNDARY (documented, intentional): this is NOT community aggregation.
    Prompts 08 owns that surface (GuideConfirmation, "This worked for me
    in [ward]" on the GUIDE itself); v1 keeps the assistant's click as a
    per-run flag — no content-bearing write, no counts shown. A member can
    only mark their own run (the lookup filters ``user=request.user``;
    someone else's run is a 404, never a leak).
    """
    run = get_object_or_404(AssistantRun, pk=pk, user=request.user)
    assistant.mark_helpful(run)
    return render(
        request,
        "ai/ask.html",
        {
            "form": AskAssistantForm(),
            "runs_used": assistant.runs_used_today(request.user),
            "runs_limit": assistant.ASSISTANT_DAILY_RUNS_PER_USER,
            "runs_remaining": assistant.quota_remaining(request.user),
            "result": None,
            "state_message": "",
            "verify_thanks": True,
        },
    )
