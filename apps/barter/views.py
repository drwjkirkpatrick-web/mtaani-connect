"""Barter views (Prompt 21) — compose, trail-detail, inbox; POST-only moves.

STATE RULE (spec verbatim): views NEVER write status fields — every move
goes through services (accept / cancel / complete / send), which drive
models.Offer.transition(). A service refusal (state machine or roles)
comes back as a flash message with the model's plain-language reason; the
user stays on the negotiation and nothing half-applies.

IDOR CONTRACT (messaging precedent): every offer-scoped route resolves
through services.get_offer_for and answers 404 (NOT 403) for
non-parties — a distinguishable error would confirm to an ID guesser that
the offer exists. Anonymous users get the ordinary login_required
redirect.

NO-CUSTODY COPY: wherever a cash figure renders, the honest line is
"You two settle the cash part yourselves (M-Pesa) — the app never holds
money." (gettext'd in templates; this module carries no custody logic).
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.core.types import OfferStatus  # noqa: F401 — state names (vocabulary)

from . import services
from .forms import OfferDraftEditForm, OfferForm, item_rows
from .models import Offer, OfferSide, OfferStatusError

PAGE_SIZE = 20  # inbox rows per page — the 50 KB mobile budget's pacing cap

DRAFT = OfferStatus.DRAFT.value
OFFERED = OfferStatus.OFFERED.value

CASH_HONESTY_NOTE = _(
    "You two settle the cash part yourselves (M-Pesa) — the app never "
    "holds money."
)


def _plain(exc) -> str:
    """First human-readable message of a ValidationError-family refusal.

    WHY: the flash channel shows the model's plain-language reason (the
    tested words), not an HTTP-exception trace or a generic 'invalid'.
    """
    given = getattr(exc, "messages", None)
    if given:
        return str(given[0])
    given = getattr(exc, "message_dict", None)
    if given:
        for values in given.values():
            if values:
                return str(values[0])
    return str(exc)


def _offer_or_404(request, pk):
    row = services.get_offer_for(request.user, pk)
    if row is None:
        raise Http404
    return row


def _compose_ctx(request, *, form, give_set, want_set, heading, post_to, extra=None):
    """Compose-page render context. `post_to` is the READY url (views
    reverse it themselves — the counter form posts to ITS parent's route,
    which a bare {% url %} cannot carry)."""
    ctx = {
        "form": form,
        "give_set": give_set,
        "want_set": want_set,
        "heading": heading,
        "post_to": post_to,
        "cash_note": CASH_HONESTY_NOTE,
    }
    ctx.update(extra or {})
    return ctx


def _side_rows(formset):
    """A formset's [(item, qty), ...] — blank slots pass through for the
    service to drop; caps and refusals live in services (the ONE door)."""
    return [
        (row.get("item") or "", row.get("qty") or "")
        for row in getattr(formset, "cleaned_data", [])
        if isinstance(row, dict)
    ]


# ---------------------------------------------------------------- inbox


@login_required
def offer_list(request):
    """The barter inbox (route 'list'): offers waiting on the member,
    offers they put on the table, their drafts, and recently settled
    deals.

    WHY per-row tip checks instead of clever annotations: the tip is the
    supersedence path's end (services.tip_of) — correctness first; the
    page paces at PAGE_SIZE rows for the mobile budget. Scale moves of
    this query land with the parent's consolidation pass, not here.
    """
    user = request.user
    incoming: list = []
    for row in (
        Offer.objects.filter(counterpart=user, status=OFFERED)
        .select_related("initiator", "counterpart")
        .order_by("-updated_at")[:PAGE_SIZE]
    ):
        tip = services.tip_of(row)
        if tip is not None and tip.pk == row.pk and tip.counterpart_id == user.pk:
            incoming.append(row)
    outgoing = list(
        Offer.objects.filter(initiator=user, status=OFFERED)
        .select_related("counterpart")
        .order_by("-updated_at")[:PAGE_SIZE]
    )
    drafts = list(services.my_drafts(user)[:PAGE_SIZE])
    settled = list(services.completed_deals_for(user)[:PAGE_SIZE])
    return render(
        request,
        "barter/list.html",
        {
            "incoming": _decorate(incoming),
            "outgoing": _decorate(outgoing),
            "drafts": _decorate(drafts),
            "settled": _decorate(settled),
        },
    )


def _decorate(rows) -> list[dict]:
    """Row render dicts (terms summary for the inbox cards)."""
    out = []
    for row in rows:
        out.append(
            {
                "row": row,
                "give": [i.item for i in row.items.filter(side=OfferSide.GIVE.value)],
                "want": [i.item for i in row.items.filter(side=OfferSide.WANT.value)],
                "mode": row.mode,
                "status": row.status,
            }
        )
    return out


# ------------------------------------------------------------- compose


def _listing_guard(request, origin_ref):
    """Resolve a ?listing= origin defensively. Returns (origin_id, title)
    or an HONEST render response (unavailable / own listing)."""
    if not origin_ref:
        return None, None, None
    try:
        owner, title = services.resolve_listing_offer(origin_ref)
    except services.ListingUnavailable as exc:
        return None, None, render(
            request, "barter/listing_unavailable.html", {"reason": exc.message}
        )
    if owner.pk == request.user.pk:
        # No self-deal: the honest line, not a form error on an empty POST.
        return None, None, render(
            request, "barter/listing_unavailable.html", {"own_listing": True}
        )
    return int(origin_ref), title, None


@login_required
def offer_new(request):
    """Compose a ROOT offer (route 'new'). Optional ?listing=<pk>
    (marketplace origin) resolves through listings' PUBLIC door. POST
    creates the DRAFT version and redirects to its detail — send happens
    there ON PURPOSE (drafts are the edit window; items/cash/mode stay
    editable until the offer is on the table)."""
    origin_id, listing_title, guard = _listing_guard(
        request, request.GET.get("listing", "").strip()
    )
    if guard is not None:
        return guard
    give_set = item_rows("give", data=request.POST or None)
    want_set = item_rows("want", data=request.POST or None)
    form = OfferForm(
        request.POST or None,
        # The listing origin must SURVIVE a failed POST (hidden input)
        # and pre-fill on GET — compose keeps the origin context round-trip.
        initial=None if request.method == "POST" else {"origin_listing": origin_id},
    )
    if request.method == "POST" and form.is_valid() and give_set.is_valid() and want_set.is_valid():
        cleaned = form.cleaned_data
        counterpart = None
        try:
            o_id = None
            if cleaned.get("origin_listing") is not None:
                owner, listing_title = services.resolve_listing_offer(
                    cleaned["origin_listing"]
                )
                counterpart = owner
                o_id = cleaned["origin_listing"]
            else:
                User = get_user_model()
                counterpart = User.objects.filter(
                    username__iexact=cleaned.get("counterpart_username") or ""
                ).first()
                if counterpart is None:
                    raise ValidationError(
                        _("No member with that username — check the spelling.")
                    )
            offer = services.create_offer(
                initiator=request.user,
                counterpart=counterpart,
                mode=cleaned["mode"],
                cash_amount=cleaned.get("cash_amount"),
                give_items=_side_rows(give_set),
                want_items=_side_rows(want_set),
                origin_listing_id=o_id,
            )
            messages.success(
                request,
                _("Draft saved — it lands on their table only when you send it."),
            )
            return redirect("barter:detail", pk=offer.pk)
        except (ValidationError, OfferStatusError, services.ListingUnavailable) as exc:
            if isinstance(exc, services.ListingUnavailable):
                return render(
                    request,
                    "barter/listing_unavailable.html",
                    {"reason": exc.message},
                )
            messages.error(request, _plain(exc))
    return render(
        request,
        "barter/compose.html",
        _compose_ctx(
            request,
            form=form,
            give_set=give_set,
            want_set=want_set,
            heading=_("Offer a trade"),
            post_to=reverse("barter:new"),
            extra={"listing_title": listing_title, "origin_id": origin_id},
        ),
    )


def _edit_form(request, offer, *, bound: bool):
    """The draft-edit form binds mode/cash; counterpart and origin are
    FIXED at creation (a counter answers a concrete member/terms)."""
    data = request.POST if bound else None
    initial = None if bound else {
        "mode": offer.mode,
        "cash_amount": offer.cash_amount,
    }
    return OfferDraftEditForm(data, initial=initial)


def _initial_rows(offer):
    """Existing DRAFT rows as formset initials (edit-in-DRAFT parity)."""
    give = list(offer.items.filter(side=OfferSide.GIVE.value))
    want = list(offer.items.filter(side=OfferSide.WANT.value))
    extra = max(3 - len(give), 1), max(3 - len(want), 1)
    give_set = item_rows("give", extra=extra[0],
        initial=[{"item": i.item, "qty": i.qty} for i in give]
    )
    want_set = item_rows("want", extra=extra[1],
        initial=[{"item": i.item, "qty": i.qty} for i in want]
    )
    return give_set, want_set


@login_required
def offer_edit(request, pk):
    """Edit YOUR OWN draft (route 'edit') — items per side, mode, cash.
    NOT a draft → straight back to the detail page (the edit CTA only
    exists for drafts; the service enforces the same wall)."""
    offer = _offer_or_404(request, pk)
    if offer.status != DRAFT or offer.initiator_id != request.user.pk:
        messages.error(request, _("Offers can only be changed while they are still a draft."))
        return redirect("barter:detail", pk=pk)
    give_set = want_set = None
    if request.method == "POST":
        give_set = item_rows("give", data=request.POST)
        want_set = item_rows("want", data=request.POST)
        form = _edit_form(request, offer, bound=True)
        if form.is_valid() and give_set.is_valid() and want_set.is_valid():
            cleaned = form.cleaned_data
            try:
                services.update_draft(
                    offer,
                    by_user=request.user,
                    mode=cleaned["mode"],
                    cash_amount=cleaned.get("cash_amount"),
                    give_items=_side_rows(give_set),
                    want_items=_side_rows(want_set),
                )
            except (ValidationError, OfferStatusError) as exc:
                messages.error(request, _plain(exc))
            else:
                messages.success(request, _("Draft updated."))
                return redirect("barter:detail", pk=offer.pk)
    if give_set is None:
        give_set, want_set = _initial_rows(offer)
        form = _edit_form(request, offer, bound=False)
    return render(
        request,
        "barter/compose.html",
        _compose_ctx(
            request,
            form=form,
            give_set=give_set,
            want_set=want_set,
            heading=_("Edit your draft"),
            post_to=reverse("barter:edit", args=[offer.pk]),
            extra={"edit_draft": offer},
        ),
    )


# ---------------------------------------------------- detail + the moves


@login_required
def offer_detail(request, pk):
    """The negotiation page (route 'detail'): the lineage trail root →
    current terms, the state badge, and NOTHING but the moves the RIGHT
    party may make in the CURRENT state (services.actions_for decides;
    the POST routes enforce the same policy server-side)."""
    row = _offer_or_404(request, pk)
    tip = services.tip_of(row)
    trail = services.lineage_trail(row)
    actions = services.actions_for(row, request.user)
    versions = []
    for version in trail:
        versions.append(
            {
                "row": version,
                "give": list(version.items.filter(side=OfferSide.GIVE.value)),
                "want": list(version.items.filter(side=OfferSide.WANT.value)),
            }
        )
    return render(
        request,
        "barter/detail.html",
        {
            "row": row,
            "tip": tip,
            "trail": versions,
            "actions": actions,
            "cash_note": CASH_HONESTY_NOTE,
            "give_side": OfferSide.GIVE.value,
            "want_side": OfferSide.WANT.value,
        },
    )


@login_required
def offer_counter(request, pk):
    """Start a COUNTER on the offer on the table (route 'counter'): GET
    shows the current terms + an empty compose (mode prefilled from the
    terms; cash/items NOT — the counterer writes their own sides; no
    counterpart picker — a counter ANSWERS the maker of the current
    terms). POST creates/updates the counter DRAFT child, ready to send
    from its own detail page — SEND is what takes the parent OFFERED →
    COUNTERED (services.send_offer)."""
    row = _offer_or_404(request, pk)
    tip = services.tip_of(row)
    if tip is None or tip.status != OFFERED:
        messages.error(request, _("That offer is no longer on the table."))
        return redirect("barter:detail", pk=row.pk)
    if tip.counterpart_id != request.user.pk:
        messages.error(
            request,
            _("Only the member the offer was made to can counter it."),
        )
        return redirect("barter:detail", pk=row.pk)
    bound = request.method == "POST"
    form = OfferDraftEditForm(request.POST if bound else None,
                              initial=None if bound else {"mode": tip.mode})
    give_set = item_rows("give", data=request.POST if bound else None)
    want_set = item_rows("want", data=request.POST if bound else None)
    if bound and form.is_valid() and give_set.is_valid() and want_set.is_valid():
        cleaned = form.cleaned_data
        try:
            child = services.create_counteroffer(
                parent=tip,
                counterer=request.user,
                mode=cleaned["mode"],
                cash_amount=cleaned.get("cash_amount"),
                give_items=_side_rows(give_set),
                want_items=_side_rows(want_set),
            )
        except (ValidationError, OfferStatusError, services.ListingUnavailable) as exc:
            messages.error(request, _plain(exc))
        else:
            messages.success(
                request,
                _("Counter draft saved — send it to put your terms on the table."),
            )
            return redirect("barter:detail", pk=child.pk)
    return render(
        request,
        "barter/compose.html",
        _compose_ctx(
            request,
            form=form,
            give_set=give_set,
            want_set=want_set,
            heading=_("Counter this offer"),
            post_to=reverse("barter:counter", args=[row.pk]),
            extra={
                "counter_mode": True,
                "counter_against": tip,
                "listing_title": None,
                "origin_id": None,
            },
        ),
    )


def _post_move(request, pk, move):
    """Shared POST orchestrator for the state moves (send/accept/cancel/
    complete): resolve via the membership gate, run the SERVICE, flash
    the plain-language refusal (or quiet success) and return to the
    trail. 405s on GET come from require_POST — state changes are never
    link-followed. WHY back-to-detail ALWAYS: the audit trail is the
    page users reason from; a half-finished attempt shows there."""
    row = _offer_or_404(request, pk)
    try:
        move(row, by_user=request.user)
    except (ValidationError, OfferStatusError) as exc:
        messages.error(request, _plain(exc))
        return redirect("barter:detail", pk=row.pk)
    except services.ListingUnavailable:  # never in a move — kept for shape
        messages.error(request, _("That listing isn't available."))
    return redirect("barter:detail", pk=row.pk)


@login_required
@require_POST
def offer_send(request, pk):
    """Send a draft (route 'send'): DRAFT → OFFERED by the maker; for a
    counter child, the parent goes OFFERED → COUNTERED in the same call."""
    return _post_move(request, pk, lambda row, by_user: services.send_offer(row, by_user=by_user))


@login_required
@require_POST
def offer_accept(request, pk):
    """Accept (route 'accept'): the CURRENT terms OFFERED → ACCEPTED by
    the member they were made to."""
    return _post_move(request, pk, lambda row, by_user: services.accept(row, by_user=by_user))


@login_required
@require_POST
def offer_cancel(request, pk):
    """Cancel (route 'cancel'): either party walks the whole negotiation
    away → CANCELLED (terminal)."""
    return _post_move(request, pk, lambda row, by_user: services.cancel(row, by_user=by_user))


@login_required
@require_POST
def offer_complete(request, pk):
    """Complete (route 'complete'): ACCEPTED → COMPLETED by either party
    (the stated handoff happened; the app held nothing, ever)."""
    return _post_move(request, pk, lambda row, by_user: services.complete(row, by_user=by_user))
