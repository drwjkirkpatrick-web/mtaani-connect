"""Content-language preference model (PROMPTS.md Prompt 35).

The spec: users read content in sw, en, or both — the DEFAULT is 'both'
because mixed feeds are the honest default in a community app. The chrome
language (en/sw) lives elsewhere (django_language cookie, Prompt 05); this
preference is about WHAT CONTENT to show, not what the buttons say.

Anonymous visitors keep the preference in the session; signed-in members
get a durable row here (one row per user, upserted).
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _lazy

from apps.core.i18n import CONTENT_LANG_CHOICES, DEFAULT_CONTENT_LANG


class ContentLanguagePreference(models.Model):
    """One row per user: which content language to read."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="content_lang_pref",
        primary_key=True,
        verbose_name=_lazy("Member"),
    )
    value = models.CharField(
        _lazy("Content language"),
        max_length=8,
        choices=CONTENT_LANG_CHOICES,
        default=DEFAULT_CONTENT_LANG,
    )
    updated_at = models.DateTimeField(_lazy("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _lazy("Content language preference")
        verbose_name_plural = _lazy("Content language preferences")

    def __str__(self):
        return f"{self.user_id}:{self.value}"
