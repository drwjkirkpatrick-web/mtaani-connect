"""App config for the Traffic app (Prompt 32).

WHY: the name must be "apps.traffic" so Django resolves the same module
the parent already wired into INSTALLED_APPS and the root URLconf
(``path("traffic/", include("apps.traffic.urls"))`` in mtaani/urls.py).

NOTE: NO model in this app — the planner is deliberately STATELESS (pure
inference at read time over routes + crowd public data). Nothing is
cached between requests in v1; a disruption cache would need a model and
a migration, so we skip it and document that decision instead.
"""

from django.apps import AppConfig


class TrafficConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.traffic"
    verbose_name = "Traffic & route planning"
