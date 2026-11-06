"""Group create form (Prompt 13). Only CREATE exists in V1 — group editing
can land later without touching a single route (services own the rules;
the model's MaxLengthValidator makes the 500-char ceiling a form error
automatically, not just a service error)."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Group


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("name", "kind", "ward", "description")
        labels = {
            "name": _("Group name"),
            "kind": _("Type of group"),
            "ward": _("Ward (coarse area)"),
            "description": _("Description (up to 500 characters)"),
        }
