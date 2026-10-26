"""Media routes (Prompt 06).

NOTE: /media/upload/ is an app route; prod static file serving of
MEDIA_ROOT at /media/ (Caddy, Prompt 42) must route /media/upload/
to Django first — deployment runbook covers it.
"""
from django.urls import path

from . import views

app_name = "media"

urlpatterns = [
    path("upload/", views.upload_view, name="upload"),
]
