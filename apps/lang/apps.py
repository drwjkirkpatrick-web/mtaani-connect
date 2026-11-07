"""apps.lang AppConfig (Prompt 35). Parent pre-wired it in INSTALLED_APPS."""
from django.apps import AppConfig


class LangConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lang"
    verbose_name = "Content language preferences"
