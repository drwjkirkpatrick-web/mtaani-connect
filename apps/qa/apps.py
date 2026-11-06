"""Q&A app: community questions, answers, AI-answer labeling, verification votes."""

from django.apps import AppConfig


class QaConfig(AppConfig):
    # WHY: explicit name so the parent's INSTALLED_APPS wiring ('apps.qa') resolves.
    name = "apps.qa"
    verbose_name = "Community Q&A"
    default_auto_field = "django.db.models.BigAutoField"
