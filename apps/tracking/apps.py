"""Django app config for the Tracking app (Prompt 27 — the privacy core).

WHY the name must be "apps.tracking": the parent already wired
``"apps.tracking"`` into INSTALLED_APPS and ``path("tracking/",
include("apps.tracking.urls"))`` into mtaani/urls.py — Django must resolve
exactly this module.
"""

from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tracking"
    verbose_name = "Authorized tracking sessions (privacy core)"
