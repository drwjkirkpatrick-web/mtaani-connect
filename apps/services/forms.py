"""Service form (Prompt 18) — the KDPA-shaping surface.

Deliberately NO address/phone/URL field, mirroring apps.listings:
- location is ward/neighbourhood text only; the coordinate guard lives on
  the model clean() so admin/scripts inherit it, not just this form;
- contact happens through in-app messaging (?service=<pk> enquiry, Prompt
  20) — no phone number is ever collected or shown;
- the rate-type matrix is enforced on the model clean(); the help texts
  make each rule legible before the error appears.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import (
    MAX_AVAILABILITY_CHARS,
    MAX_DESC_CHARS,
    MAX_TITLE_CHARS,
    RateType,
    ServiceListing,
)


class ServiceForm(forms.ModelForm):
    class Meta:
        model = ServiceListing
        fields = [
            "title",
            "title_sw",
            "description",
            "description_sw",
            "category",
            "rate_type",
            "rate_amount",
            "availability_notes",
            "ward",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "maxlength": MAX_TITLE_CHARS,
                    "placeholder": _("Math tutoring, Form 2–4, evening…"),
                }
            ),
            "title_sw": forms.TextInput(
                attrs={"maxlength": MAX_TITLE_CHARS}
            ),
            "description": forms.Textarea(
                attrs={
                    "maxlength": MAX_DESC_CHARS,
                    "rows": 5,
                    "placeholder": _(
                        "What exactly is the service, what does it cover?"
                    ),
                }
            ),
            "description_sw": forms.Textarea(attrs={"rows": 3}),
            "rate_amount": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
            "availability_notes": forms.Textarea(
                attrs={"maxlength": MAX_AVAILABILITY_CHARS, "rows": 3}
            ),
            "ward": forms.TextInput(
                attrs={
                    "maxlength": 80,
                    "placeholder": _("Ward or neighbourhood only — e.g. Kilimani"),
                }
            ),
        }
        labels = {
            "title": _("Title"),
            "title_sw": _("Title (Kiswahili)"),
            "description": _("Description"),
            "description_sw": _("Description (Kiswahili)"),
            "category": _("Category"),
            "rate_type": _("Rate type"),
            "rate_amount": _("Rate (KSh)"),
            "availability_notes": _("Availability notes"),
            "ward": _("Ward / neighbourhood"),
        }
        help_texts = {
            "rate_type": _(
                "Hourly: charge per hour. Fixed: one price for the job. "
                "Negotiable: agree the rate in your messages."
            ),
            "rate_amount": _(
                "Required for hourly and fixed rates; leave empty (and the "
                "rate type negotiable) when the rate is open."
            ),
            "availability_notes": _(
                "e.g. “Weekdays 5–8 pm, Saturday mornings.” Plain notes only."
            ),
            "ward": _(
                "Ward or neighbourhood ONLY — never a street address or "
                "coordinates; this page is public."
            ),
            "description": _(
                "Plain text only — no phone numbers or WhatsApp lines: "
                "clients message you through the app."
            ),
        }
