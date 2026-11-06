"""Forms for the crowd app (Prompt 30).

The model's full_clean() stays the real gate; the form shapes the intake
(stage + kind required, note/hint optional) and refuses an unknown type
at the field level, so users get inline errors instead of server errors.
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import MAX_HINT_CHARS, MAX_NOTE_CHARS, ReportType

from apps.routes.models import Route, Stage


class ReportForm(forms.ModelForm):
    kind = forms.ChoiceField(
        label=_("What are you seeing here?"),
        choices=ReportType.choices,
        widget=forms.Select,
    )
    stage = forms.ModelChoiceField(
        label=_("Stage"),
        queryset=Stage.objects.order_by("name"),
        help_text=_("Pick the stage you are standing at — reports are events at stages, never guessed positions."),
    )
    route = forms.ModelChoiceField(
        label=_("Route (optional)"),
        queryset=Route.objects.order_by("name_en"),
        required=False,
        help_text=_("Pick the route if your report mentions one."),
    )
    note = forms.CharField(
        label=_("Note (optional)"),
        required=False,
        max_length=MAX_NOTE_CHARS,
        help_text=_("%(chars)s characters max — plain, factual."),
    )
    destination_hint = forms.CharField(
        label=_("Heading towards (optional)"),
        max_length=MAX_HINT_CHARS,
        required=False,
        strip=True,
        help_text=_('Only for "matatu here now" — plain text, never coordinates.'),
    )

    class Meta:
        from .models import CrowdReport

        model = CrowdReport
        fields = ("kind", "stage", "route", "note", "destination_hint")
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["note"].help_text %= {"chars": MAX_NOTE_CHARS}
