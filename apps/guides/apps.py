"""NOTE: Django app config for the Guides app ("WikiHow for Kenya").

WHY: The name must be "apps.guides" so Django resolves the same module the
parent already wired into INSTALLED_APPS and the URL include in mtaani/urls.py
(``path("guides/", include("apps.guides.urls"))``).
"""

from django.apps import AppConfig


class GuidesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.guides"
    verbose_name = "Guides (local knowledge)"
