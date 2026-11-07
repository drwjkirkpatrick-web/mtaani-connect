"""URL routes for the AI app (PROMPTS.md Prompt 33).

app_name = "ai" — namespaced so templates/tests reverse ``ai:…``; the
parent-wired include sits at ``/ai/`` in mtaani/urls.py (not touched
here). The Prompt 33 bookkeeping routes are staff-only (decorators on
the views): the ledger is spend/health bookkeeping, never a member
feature, and nothing on this app publishes content. The Prompt 34
"Ask Mtaani" routes are MEMBER surfaces (login_required — the per-user
daily quota and the verify click need the member), still draft-only.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "ai"

urlpatterns = [
    # /ai/ → the status page (a friendly root for the namespace).
    path("", views.index, name="index"),
    # The admin-ish page: today's spend, remaining, recent ledger rows.
    path("status/", views.status_page, name="status_page"),
    # The JSON mirror for dashboards (named ``ai:status`` per the parent
    # note; same numbers as the page — one shared _ai_status() truth).
    path("status.json", views.status_json, name="status"),
    # POST-only: the mock echo probe (ONE Mock provider call, labelled).
    path("probe/", views.mock_probe, name="probe"),
    # Prompt 34 — "Ask Mtaani": the member-facing local-adaptation
    # assistant (GET = guided intake form + honest quota meter;
    # POST = quota → retrieval → ledgered run_call → gated labelled
    # draft + cited guides + verify CTA).
    path("ask/", views.ask, name="ask"),
    # POST-only: the "Did this work for you?" click — logs ONE boolean
    # on the member's run row (no community aggregation in v1 — that is
    # Prompt 08's GuideConfirmation realm).
    path("ask/<int:pk>/helpful/", views.ask_helpful, name="helpful"),
]
