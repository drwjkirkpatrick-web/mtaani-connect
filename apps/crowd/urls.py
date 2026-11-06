"""URL routes for the Crowd app (Prompt 30).

app_name = "crowd" — namespaced so templates/tests reverse ``crowd:stage``;
mtaani/urls.py already includes this module under /crowd/ (parent-wired).
"""
from __future__ import annotations

from django.urls import path

from . import views

app_name = "crowd"

urlpatterns = [
    path("", views.index, name="index"),  # → crowd:mine (login-gated)
    path("stage/<int:pk>/", views.stage_reports, name="stage"),
    path("reports/new/", views.report_new, name="report_new"),
    path("mine/", views.mine, name="mine"),
]
