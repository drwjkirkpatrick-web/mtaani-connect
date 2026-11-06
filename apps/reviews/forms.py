"""Leave-a-review form (Prompt 22) — thin input surface over the service.

The form NEVER publishes: it validates input shape (stars 1–5, text cap,
1-star dispute note) before services.create_deal_review runs the deal-party
gates (which only the origin-app doors can answer). Defense in depth: the
same bounds live on the model (full_clean) and in the service — one constant,
one consumer, three enforcement layers (trust's precept).
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from .models import (
    ALLOWED_ORIGIN_APPS,
    MAX_TEXT_CHARS,
    MIN_DISPUTE_NOTE_CHARS,
    MSG_DISPUTE_NOTE_REQUIRED,
    _dispute_note_required_for,
)

STAR_CHOICES = [("", "—")] + [(i, f"{i} ★") for i in range(1, 6)]


class LeaveReviewForm(forms.Form):
    """Stars + optional text (+ required dispute note for 1-star)."""

    deal_app = forms.ChoiceField(
        choices=[(value, value) for value in ALLOWED_ORIGIN_APPS],
        label=_("Deal type"),
    )
    deal_id = forms.IntegerField(
        label=_("Deal number"), min_value=1, widget=forms.HiddenInput,
    )
    stars = forms.ChoiceField(
        choices=STAR_CHOICES, label=_("Stars"), 
    )
    text = forms.CharField(
        label=_("Your review"),
        required=False,
        max_length=MAX_TEXT_CHARS,
        widget=forms.Textarea(
            attrs={"rows": 4, "maxlength": MAX_TEXT_CHARS}
        ),
        help_text=_("Optional — at most %(n)s characters.") % {"n": MAX_TEXT_CHARS},
    )
    dispute_note = forms.CharField(
        label=_("What went wrong?"),
        required=False,
        max_length=MAX_TEXT_CHARS,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_(
            "Required for 1-star reviews — at least %(min)s characters."
        )
        % {"min": MIN_DISPUTE_NOTE_CHARS},
    )

    def clean_stars(self):
        stars = self.cleaned_data["stars"]
        return int(stars)

    def clean(self):
        cleaned = super().clean()
        stars = cleaned.get("stars") or 0
        deal_app = cleaned.get("deal_app")
        deal_id = cleaned.get("deal_id")
        note = (cleaned.get("dispute_note") or "").strip()
        if (
            stars
            and deal_app
            and deal_id
            and _dispute_note_required_for(stars, deal_app, deal_id)
            and len(note) < MIN_DISPUTE_NOTE_CHARS
        ):
            # NOTE: mirrors models.clean + the service re-check (same helper).
            self.add_error("dispute_note", MSG_DISPUTE_NOTE_REQUIRED)
        return cleaned
