"""Barter forms (Prompt 21) — the compose surface (offer + counter + draft
edit all render from these pieces).

Shape kept deliberately small: ONE offer form (deal type, stated cash,
counterpart) + TWO item-row formsets (prefixes 'give'/'want') — the two
sides of a version, "I give / I want", each an independent grid. The
business rules (mode/cash pair, caps, counterpart resolve, DRAFT-only)
are enforced in services and models; forms do field-level sanity only and
pass the raw rows on.
"""
from django import forms
from django.utils.translation import gettext_lazy as _

from apps.core.types import BarterMode

from .models import MAX_ITEM_CHARS, MAX_QTY_CHARS


class OfferItemForm(forms.Form):
    """One line of one side ("2kg maize" needs qty '2 kg'; 'sewing hour'
    may state none). required=False so blank slots don't error — the
    service drops fully-empty slots and refuses qty-without-item."""

    item = forms.CharField(
        max_length=MAX_ITEM_CHARS,
        required=False,
        label=_("Item"),
    )
    qty = forms.CharField(
        max_length=MAX_QTY_CHARS,
        required=False,
        label=_("Quantity"),
    )


class OfferDraftEditForm(forms.Form):
    """Mode + stated cash — the DRAFT-edit and COUNTER-compose surface (a
    counter's counterpart/terms-being-answered are fixed by its parent,
    so no counterpart picker belongs on this form)."""

    mode = forms.ChoiceField(choices=BarterMode.choices, label=_("Deal type"))
    cash_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
        label=_("Cash amount (KES)"),
        help_text=_(
            "You two settle the cash part yourselves (M-Pesa) — the app "
            "never holds money."
        ),
    )


def item_rows(prefix: str, data=None, initial=None, extra: int = 3):
    """A BOUND formset for one side (prefix 'give'/'want'), one item per
    row — quantity is free text ('2 kg', 'sewing hour'). required=False so
    blank slots don't error — the service drops fully-empty slots and
    refuses qty-without-item.

    WHY a binding function (not a bare class factory): the formset
    `prefix` must be passed AT INSTANTIATION — a factory-created class
    without it binds the default 'form' prefix and silently ignores the
    posted give-/want- management forms (Debug: 'ManagementForm data has
    been tampered with').
    """
    from django.forms import formset_factory

    return formset_factory(
        OfferItemForm, extra=extra, can_delete=False, max_num=10
    )(data=data, initial=initial, prefix=prefix)


class OfferForm(forms.Form):
    """Deal-type + stated cash + counterpart. The counterpart comes from
    EITHER a username (direct negotiation) OR a listing reference
    (marketplace origin) — exactly one; the view resolves the listing
    owner through listings' PUBLIC door (services.resolve_listing_offer)."""

    mode = forms.ChoiceField(choices=BarterMode.choices, label=_("Deal type"))
    cash_amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
        label=_("Cash amount (KES)"),
        help_text=_(
            "You two settle the cash part yourselves (M-Pesa) — the app "
            "never holds money."
        ),
    )
    counterpart_username = forms.CharField(
        max_length=150,
        required=False,
        label=_("Their username"),
    )
    origin_listing = forms.IntegerField(
        required=False,
        widget=forms.HiddenInput,
        label=_("Origin listing"),
    )

    def clean(self):
        cleaned = super().clean()
        username = (cleaned.get("counterpart_username") or "").strip().lstrip("@")
        listing_id = cleaned.get("origin_listing")
        if username and listing_id not in (None, ""):
            raise forms.ValidationError(
                _(
                    "Give one counterpart — a member's username or one of "
                    "their listings, not both."
                )
            )
        if not username and listing_id in (None, ""):
            raise forms.ValidationError(
                _(
                    "Say who the offer is for — a member's username, or one "
                    "of their listings."
                )
            )
        cleaned["counterpart_username"] = username
        return cleaned
