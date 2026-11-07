"""Django app config for the AI layer (PROMPTS.md Prompt 33).

Owns: the provider seam (providers.py), the token ledger + budget guard
(models.py + ledger.py), versioned prompt files (prompts/), the labeling
seam (labeling.py) and the staff-only status/probe views. Registered as
"apps.ai" by the parent in mtaani/settings/base.py and wired under /ai/
in mtaani/urls.py — neither is touched here (parent-owned files).
"""
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AiConfig(AppConfig):
    name = "apps.ai"
    verbose_name = _("AI layer")
