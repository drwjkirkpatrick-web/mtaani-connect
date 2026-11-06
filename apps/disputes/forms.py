"""Dispute forms (Prompt 23) — the member-facing open form + the moderator
proposal form. Validation lives in services/models; forms only carry
input shape and honest choices."""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ALLOWED_DEAL_APPS, MAX_CLAIM_CHARS

# The open form offers every allowed origin; services/models check that the
# opener is a party of THE deal — the form can't decide that for them.
_DEAL_APP_CHOICES = [
    (value, label)
    for value, label in (
        ("barter", _("Barter deal")),
        ("listings", _("Marketplace deal")),
        ("services", _("Service deal")),
    )
]


class OpenCaseForm(forms.Form):
    deal_app = forms.ChoiceField(choices=_DEAL_APP_CHOICES, label=_("Deal type"))
    deal_id = forms.IntegerField(
        min_value=1,
        widget=forms.HiddenInput,
        error_messages={
            "min_value": _("Give the deal this dispute is about."),
        },
    )
    what_promised = forms.CharField(
        label=_("What was promised"),
        max_length=MAX_CLAIM_CHARS,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("The deal as agreed — terms, items, cash.")}
        ),
    )
    what_happened = forms.CharField(
        label=_("What happened"),
        max_length=MAX_CLAIM_CHARS,
        widget=forms.Textarea(
            attrs={"rows": 3, "placeholder": _("Your side of what went wrong.")}
        ),
    )


class ProposalForm(forms.Form):
    action = forms.ChoiceField(
        label=_("Proposed outcome"),
        choices=[
            ("", _("— pick an outcome —")),
            ("WARN", _("Warn the member")),
            ("SUSPEND", _("Suspend the account")),
            ("REMOVE_LISTING", _("Remove the listing")),
            ("MARK_REVIEW", _("Mark for review")),
        ],
    )
    note = forms.CharField(
        label=_("Why"),
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
