"""App config for the Alerts app (Prompt 31).

WHY: the name must be "apps.alerts" so Django resolves the same module the
parent already wired into INSTALLED_APPS and the root URLconf
(``path("alerts/", include("apps.alerts.urls"))`` in mtaani/urls.py).
"""

from django.apps import AppConfig


class AlertsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.alerts"
    verbose_name = "Arrival estimates & follow alerts"
