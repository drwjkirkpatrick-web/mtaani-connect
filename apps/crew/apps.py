"""App config for the crew trip page (Prompt 29)."""
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CrewConfig(AppConfig):
    name = "apps.crew"
    verbose_name = _("Crew trip page")  # the ONE low-data crew surface

    # Convention even though the app is stateless (no models) — keeps any
    # future model addition from inheriting a default surprise.
    default_auto_field = "django.db.models.BigAutoField"
