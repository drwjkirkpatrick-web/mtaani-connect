"""App config for analytics (Prompt 37 — aggregate-only ops metrics)."""
from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "Analytics (aggregate-only)"
