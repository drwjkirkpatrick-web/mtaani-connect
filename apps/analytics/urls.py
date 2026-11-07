"""Staff analytics dashboards (Prompt 37) — URLs.

NOTE: every surface is staff- or role-gated at the VIEW (tests pin the
scoping). Aggregate-only everywhere — no per-user route exists on purpose
(PROMPTS.md Prompt 37, DPA discipline).
"""
from django.urls import path

from apps.analytics import views

app_name = "analytics"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("queue/", views.queue, name="queue"),
    path("sacco/", views.sacco, name="sacco"),
    path("status/", views.status, name="status"),
]
