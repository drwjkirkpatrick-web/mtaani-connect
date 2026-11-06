"""Trust app config (Prompt 16) — 'apps.trust' is parent-prewired."""

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class TrustConfig(AppConfig):
    # WHY: explicit name so the parent's INSTALLED_APPS wiring ('apps.trust')
    # resolves exactly (same pattern as qa/moderation).
    name = "apps.trust"
    verbose_name = _("Trust signals")
    default_auto_field = "django.db.models.BigAutoField"
