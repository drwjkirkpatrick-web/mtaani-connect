"""URL routes for the Moderation app (Prompt 15).

app_name = "moderation" — reverse() as e.g. moderation:report_create.
mtaani/urls.py includes this module under /moderation/ (parent-owned).
"""
from django.urls import path

from . import views

app_name = "moderation"

urlpatterns = [
    path("", views.queue, name="queue"),  # GET /moderation/
    path("reports/create/", views.report_create, name="report_create"),
    path("reports/<int:pk>/resolve/", views.report_resolve, name="resolve"),
    path("suspensions/", views.suspension_draft, name="draft"),
    path("suspensions/<int:pk>/confirm/", views.suspension_confirm, name="confirm"),
    path("block/", views.block, name="block"),
    path("unblock/", views.unblock, name="unblock"),
]
