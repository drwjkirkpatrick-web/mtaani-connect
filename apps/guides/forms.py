"""Guide + step-builder forms (Prompt 08).

NOTE: like profiles, these are plain forms and the SERVICES own every write —
validation logic lives on the MODEL clean() (the real gate); the form only
duplicates the safety-warning rule so the user gets an inline error instead
of a server error.
"""
from django import forms
from django.forms import modelformset_factory
from django.utils.translation import gettext_lazy as _

from apps.core.types import SafetyCategory

from .models import (
    RISKY_SAFETY_CATEGORIES,
    SKILL_LEVELS,
    GUIDE_CATEGORIES,
    GuideStep,
)


class GuideForm(forms.Form):
    title_en = forms.CharField(label=_("Title (English)"), max_length=200)
    title_sw = forms.CharField(
        label=_("Title (Kiswahili)"),
        max_length=200,
        required=False,
        help_text=_("Leave empty to show the English title in its place."),
    )
    category = forms.ChoiceField(label=_("Category"), choices=GUIDE_CATEGORIES)
    skill_level = forms.ChoiceField(label=_("Skill level"), choices=SKILL_LEVELS)
    safety_category = forms.ChoiceField(
        label=_("Safety handling"),
        choices=SafetyCategory.choices,
        initial=SafetyCategory.NONE.value,
    )
    safety_warning = forms.CharField(
        label=_("Safety warning"),
        required=False,
        widget=forms.Textarea,
        help_text=_(
            "Required for medical, electrical or construction guides."
        ),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("safety_category") in RISKY_SAFETY_CATEGORIES and not (
            cleaned.get("safety_warning") or ""
        ).strip():
            self.add_error(
                "safety_warning",
                _(
                    "A safety warning is required for medical, electrical or "
                    "construction guides."
                ),
            )
        return cleaned


class StepForm(forms.ModelForm):
    """One row of the step builder. `image_upload` is the only file field —
    the service routes it through apps.media.save_upload (Prompt 06), so no
    image ever bypasses EXIF stripping / compression."""

    image_upload = forms.ImageField(label=_("Step photo (optional)"), required=False)
    # WHY an explicit override: the model form generated for PositiveIntegerField
    # would allow 0, but steps start at 1 (model clean) — catching it here means
    # an inline form error instead of a server error.
    position = forms.IntegerField(label=_("Position"), min_value=1)

    class Meta:
        model = GuideStep
        fields = ["position", "text"]
        labels = {"position": _("Position"), "text": _("What to do")}
        widgets = {"text": forms.Textarea(attrs={"rows": 2})}


class BaseStepFormSet(forms.BaseModelFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        positions = [
            form.cleaned_data.get("position")
            for form in self.forms
            if form.cleaned_data.get("text", "")
        ]
        if len(positions) != len(set(positions)):
            raise forms.ValidationError(
                _("Two steps have the same position number — renumber the steps.")
            )


def _steps_formset(extra: int):
    return modelformset_factory(
        GuideStep, form=StepForm, formset=BaseStepFormSet, extra=extra, can_delete=False
    )


# WHY two factories: a fresh guide gets 3 blank rows; an edit gets 2 (the
# current steps are prefilled as initial rows). Bound POSTs never persist
# half-written rows — empty text rows are skipped by the service.
STEP_FORMSET_CREATE = _steps_formset(extra=3)
STEP_FORMSET_EDIT = _steps_formset(extra=2)


class ConfirmationForm(forms.Form):
    """'This worked for me in [ward]' — ward is coarse plain text (KDPA)."""

    ward = forms.CharField(label=_("Ward where this worked"), max_length=64)
    note = forms.CharField(
        label=_("Details (optional)"), required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
