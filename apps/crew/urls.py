"""URL routes for the Crew app (Prompt 29).

app_name = "crew" — namespaced (fleet/tracking pattern) so templates and
tests reverse "crew:trip" etc. mtaani/urls.py is PARENT-OWNED and includes
this module at consolidation; tests/test_crew_flow.py mounts apps.crew.
tests_urls.py (this app's own TEST-ONLY URLconf) in the meantime.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "crew"

urlpatterns = [
    # The ONE crew page (four honest states on one render):
    path("trip/", views.trip, name="trip"),
    # Status partial (JSON) — what a reloaded tab/phone reads:
    path("trip/status/", views.trip_status, name="trip_status"),
    # Start a trip: POST → renders the ONE-TIME token page directly:
    path("trip/start/", views.trip_start, name="trip_start"),
    # End the trip whose id sits in the URL (27's session):
    path("trip/<int:session_id>/end/", views.trip_end, name="trip_end"),
]
