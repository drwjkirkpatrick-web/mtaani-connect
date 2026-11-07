"""URL routes for the Alerts app (Prompt 31).

app_name = "alerts" — namespaced so templates/tests reverse ``alerts:…``;
mtaani/urls.py already includes this module under /alerts/ (parent-wired).

Kinds in URLs: ``vehicle`` / ``route`` — the two follow kinds (both need
the same one-tap actions: toggle notify, set stage, set radius, remove).
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "alerts"

urlpatterns = [
    path("", views.index, name="index"),  # → alerts:follows (login-gated)
    path("follows/", views.my_follows, name="follows"),
    path("follows/vehicle/", views.follow_vehicle, name="follow_vehicle"),
    path("follows/route/", views.follow_route, name="follow_route"),
    path("follows/<str:kind>/<int:pk>/remove/", views.remove_follow, name="remove_follow"),
    path("follows/<str:kind>/<int:pk>/notify/", views.toggle_notify, name="toggle_notify"),
    path("follows/<str:kind>/<int:pk>/stage/", views.set_stage, name="set_stage"),
    path("follows/<str:kind>/<int:pk>/radius/", views.set_radius, name="set_radius"),
    path(
        "arrivals/<int:route_id>/<int:stage_id>/",
        views.stage_arrivals_page,
        name="arrivals",
    ),
    # The JSON mirror of the arrivals page (same rows, same labels).
    path(
        "arrivals/<int:route_id>/<int:stage_id>/status.json",
        views.stage_arrivals_json,
        name="status",
    ),
]
