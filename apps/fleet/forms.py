"""Forms for the fleet operator surfaces (Prompt 26).

Server-side tenancy, in writing: fields whose values could reach ANOTHER
SACCO's data come from a queryset limited to the requesting admin's SACCOs.
Django's ModelChoiceField/MultipleChoiceField re-validate submitted pks
against that queryset — so a forged POST carrying sacco=B.id dies in the
form, not in the database. The real authorization also re-runs in
services (defence in depth; form errors are just nicer to read).
"""
from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.fleet.services import saccos_of_admin
from apps.routes.models import Route

from .models import CrewRole, Vehicle


class VehicleForm(forms.ModelForm):
    """Register a vehicle under ONE of the requester's SACCOs."""

    class Meta:
        model = Vehicle
        fields = ("sacco", "plate", "nickname", "capacity", "routes")
        help_texts = {}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # THE tenant wall, at the form layer too: the sacco picker offers
        # ONLY saccos this user administers, and forgery re-validates here.
        self.fields["sacco"].queryset = saccos_of_admin(user)
        self.fields["routes"].queryset = Route.objects.order_by("name_en")
        self.fields["routes"].required = False
        self.fields["nickname"].required = False


class CrewRequestForm(forms.Form):
    """A member asks for a crew seat on an ACTIVE vehicle (self-request)."""

    role = forms.ChoiceField(
        label=_("Crew role"), choices=CrewRole.choices,
        initial=CrewRole.DRIVER.value,
    )
    vehicle = forms.ModelChoiceField(
        # Plates are public business data; personal contact info is not here.
        label=_("Vehicle"), queryset=None, empty_label=_("Pick the vehicle (plate)")
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services import active_vehicles  # no import cycle at module load

        self.fields["vehicle"].queryset = active_vehicles()
