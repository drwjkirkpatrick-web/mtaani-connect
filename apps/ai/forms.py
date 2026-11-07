"""Assistant forms (Prompt 34) — the guided-intake questions.

The Prompt 34 intake is two plain questions ("What do you have? What can
you get at the duka?"): a form gives honest field-level validation + bound
lengths (mobile data is the user's money) and keeps the view thin.
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from . import assistant


class AskAssistantForm(forms.Form):
    """The guided intake for "Ask Mtaani" — have + need, both bounded."""

    have_text = forms.CharField(
        label=_("What do you have on hand?"),
        help_text=_(
            "What you can use right now or get at the duka — a sufuria, a "
            "jiko, ughali flour, a bicycle…"
        ),
        max_length=assistant.MAX_HAVE_CHARS,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    need_text = forms.CharField(
        label=_("What do you want to do?"),
        help_text=_("The goal to adapt — e.g. cook ugali, or the guide's name."),
        max_length=assistant.MAX_NEED_CHARS,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
