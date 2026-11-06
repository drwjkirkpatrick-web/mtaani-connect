"""NOTE: Django app config for the Fleet app (Prompt 26).

WHY: the name must be "apps.fleet" so Django resolves the same module the
parent already wired into INSTALLED_APPS and the root URLconf
(``path("fleet/", include("apps.fleet.urls"))`` in mtaani/urls.py).
"""

from django.apps import AppConfig


class FleetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.fleet"
    verbose_name = "Fleet, SACCOs & crew (operator registry)"
