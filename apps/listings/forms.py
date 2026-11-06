"""Listing form (Prompt 17) — the KDPA-shaping surface.

There is deliberately NO address field, NO phone field, NO URL field:
- location is ward/neighbourhood text only (private-address guard lives on
  the model clean() so scripts/admin inherit it, not just this form);
- contact happens through in-app messaging (Prompt 20), so we never collect
  a phone number — the form hint says why, so members learn the culture;
- price OR negotiable is enforced on the model clean(); the help text makes
  the rule legible before the error ever appears.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import MAX_DESC_CHARS, MAX_TITLE_CHARS, Condition, Listing


class ListingForm(forms.ModelForm):
    class Meta:
        model = Listing
        fields = [
            "title",
            "title_sw",
            "description",
            "description_sw",
            "category",
            "condition",
            "price",
            "price_negotiable",
            "ward",
        ]
        widgets = {
            "title": forms.TextInput(
                attrs={
                    "maxlength": MAX_TITLE_CHARS,
                    "placeholder": _("Bluetooth speaker, solar panel…"),
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
                        "What is it, what state is it in, pickup point?"
                    ),
                }
            ),
            "description_sw": forms.Textarea(attrs={"rows": 3}),
            "price": forms.NumberInput(attrs={"min": "0", "step": "0.01"}),
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
            "condition": _("Condition"),
            "price": _("Price (KSh)"),
            "price_negotiable": _("Negotiable"),
            "ward": _("Ward / neighbourhood"),
        }
        help_texts = {
            "price": _("Leave empty and tick Negotiable if the price is open."),
            "price_negotiable": _("Tick for the Negotiable badge — handshake, Karibu market."),
            "ward": _(
                "Ward or neighbourhood ONLY — never a street address or "
                "coordinates; this page is public."
            ),
            "description": _(
                "Plain text only — no phone numbers or WhatsApp lines: "
                "buyers message you through the app and a phone number gets "
                "this listing flagged for review."
            ),
        }
