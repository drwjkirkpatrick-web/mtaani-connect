"""URL routes for the traffic app (Prompt 32).

app_name = "traffic" — namespaced so templates/tests reverse ``traffic:…``;
mtaani/urls.py already includes this module under /traffic/ (parent-wired).

Surfaces:
  /traffic/          → redirects to the planner (sibling app pattern)
  /traffic/plan/     → GET from/to journey planning (HTML)
  /traffic/plan.json → the exact machine mirror of the same plan
  /traffic/about/    → the honesty note (source separation in plain words)
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "traffic"

urlpatterns = [
    path("", views.plan_page, name="index"),
    path("plan/", views.plan_page, name="plan"),
    path("plan.json", views.plan_json, name="plan_json"),
    path("about/", views.about, name="about"),
]
