"""NOTE: Django app config for the Routes app (Prompt 25).

WHY: the name must be "apps.routes" so Django resolves the same module the
parent already wired into INSTALLED_APPS and the root URLconf
(``path("routes/", include("apps.routes.urls"))`` in mtaani/urls.py).
"""

from django.apps import AppConfig


class RoutesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.routes"
    verbose_name = "Routes & stages (matatu network)"
