"""URL routes for the Map app (Prompt 28).

app_name = "map" — namespaces (fleet/crowd/tracking pattern) under the
parent-wired /map/ include (mtaani/urls.py). The ONE page + the JSON feed.
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "map"

urlpatterns = [
    path("", views.route_map, name="route"),
    path("positions/", views.route_positions, name="positions"),
]
