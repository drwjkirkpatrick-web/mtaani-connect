"""URL routes for the Tracking app (Prompt 27).

app_name = "tracking" — namespaced (fleet/crowd pattern) so templates and
tests reverse "tracking:start" etc.; mtaani/urls.py already includes this
module under /tracking/ (parent-wired). NOTE: deliberately NO public map
route — Prompt 28 owns the map; 29 consumes the API functions above.
"""
from __future__ import annotations

from django.urls import path

from . import views
from .ingest import ping_batch  # Prompt 28 (additive ONLY — see ingest.py)

app_name = "tracking"

urlpatterns = [
    path("start/", views.session_start, name="start"),
    path("sessions/", views.my_sessions, name="my_sessions"),
    path("operator/", views.operator_sessions, name="operator"),
    path("session/<int:pk>/end/", views.session_end, name="end"),
    path("session/<int:pk>/rotate/", views.session_rotate, name="rotate"),
    # Prompt 28: token-authenticated crew-device ingest (NOT login-gated;
    # contract documented at the top of apps/tracking/ingest.py).
    path("ingest/<int:session_id>/", ping_batch, name="ingest"),
]
