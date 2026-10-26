"""Profile + privacy forms (Prompt 07)."""
from django import forms

from apps.core.geo import WARDS
from apps.core.types import LocationVisibility

from .models import VISIBILITY_LEVELS

WARD_CHOICES = [("", "—")] + [(ward, ward) for ward, _lat, _lon in WARDS]


def _csv_to_list(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


class ProfileForm(forms.Form):
    display_name = forms.CharField(max_length=64, required=False)
    bio = forms.CharField(widget=forms.Textarea, required=False)
    skills = forms.CharField(required=False, help_text="comma separated / kwa koma")
    interests = forms.CharField(required=False, help_text="comma separated / kwa koma")
    ward = forms.ChoiceField(choices=WARD_CHOICES, required=False)

    def clean_skills(self):
        return _csv_to_list(self.cleaned_data.get("skills", ""))

    def clean_interests(self):
        return _csv_to_list(self.cleaned_data.get("interests", ""))


class PrivacyForm(forms.Form):
    location_visibility = forms.ChoiceField(choices=LocationVisibility.choices)
    bio_visibility = forms.ChoiceField(choices=[(v, v) for v in VISIBILITY_LEVELS])
    skills_visibility = forms.ChoiceField(choices=[(v, v) for v in VISIBILITY_LEVELS])
    interests_visibility = forms.ChoiceField(choices=[(v, v) for v in VISIBILITY_LEVELS])
    location_group_visibility = forms.ChoiceField(choices=[(v, v) for v in VISIBILITY_LEVELS])
