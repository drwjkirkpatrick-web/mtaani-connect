"""Forms for the routes app (Prompt 25).

The model's clean() is the real gate; the form only shapes the correction
intake (route/stage pickers optional, note required) so users get inline
errors instead of server errors.
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import CorrectionKind, CorrectionQueue, Route, Stage


class CorrectionForm(forms.ModelForm):
    kind = forms.ChoiceField(
        label=_("What's wrong?"),
        choices=CorrectionKind.choices,
        widget=forms.Select,
    )
    route = forms.ModelChoiceField(
        label=_("Route"), queryset=Route.objects.order_by("name_en"),
        required=False, help_text=_("Pick the route if your correction mentions one."),
    )
    stage = forms.ModelChoiceField(
        label=_("Stage"), queryset=Stage.objects.order_by("name"),
        required=False, help_text=_("Pick the stage if your correction targets one."),
    )

    class Meta:
        model = CorrectionQueue
        fields = ("kind", "route", "stage", "note")
        widgets = {"note": forms.Textarea(attrs={"rows": 3, "maxlength": 500})}
