"""App config for the Crowd app (Prompt 30).

WHY: the name must be "apps.crowd" so Django resolves the same module the
parent already wired into INSTALLED_APPS and the root URLconf
(``path("crowd/", include("apps.crowd.urls"))`` in mtaani/urls.py).
"""

from django.apps import AppConfig


class CrowdConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crowd"
    verbose_name = "Passenger crowd reports"
