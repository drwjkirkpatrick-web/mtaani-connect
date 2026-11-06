"""Barter services (Prompt 21) — one door for every write + shared reads.

The layout mirrors the sibling apps (listings/messaging): views and other
apps come HERE, never to model internals or raw status writes. The state
machine lives in models.Offer.transition(); this module adds the ROLE
rules (who may do what) and the ORCHESTRATION (atomic counter-sends,
propagated settlements, the expiry sweep).

CROSS-APP CONTRACT (Hard Rules): listing reads go through listings' PUBLIC
door get_visible_listing, imported lazily — an unresolvable listing is a
handled state (ListingUnavailable), never a fabricated counterpart.

NO messaging coupling in V1: barter's lineage IS the negotiation trail;
threads remain a documented future hook (models.Offer docstring).

PUBLIC API for Prompt 22 (Reviews & Reputation) and future consumers:
- deal_completed                     -- Signal (apps/barter/signals.py);
                                        sender=Offer, kwargs: offer (the
                                        COMPLETED tip), root, parties (ids)
- completed_deals_between(a, b)      -- root Offer rows the pair settled
- completed_deals_for(user)          -- root Offer rows the member settled
- open_negotiations_for(user)        -- open lineage roots involving user
- my_drafts(user)                    -- the member's own unsent versions
- tip_of(row) / Offer.walk_to_root / Offer.lineage() -- the supersedence
                                        trail (models docstring)
The two parties of any settlement are root.initiator and
root.counterpart — the ping-pong clean() rule guarantees every row of the
lineage names the same pair.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import BarterMode, OfferStatus

from . import signals
from .models import (
    EXPIRY_DAYS,
    MAX_ITEMS_PER_SIDE,
    MSG_BAD_MODE,
    MSG_CASH_FORBIDDEN,
    MSG_CASH_REQUIRED,
    MSG_SELF_DEAL,
    Offer,
    OfferItem,
    OfferOrigin,
    OfferSide,
    OfferStatusError,
)

DRAFT = OfferStatus.DRAFT.value
OFFERED = OfferStatus.OFFERED.value
COUNTERED = OfferStatus.COUNTERED.value
ACCEPTED = OfferStatus.ACCEPTED.value
COMPLETED = OfferStatus.COMPLETED.value
CANCELLED = OfferStatus.CANCELLED.value
EXPIRED = OfferStatus.EXPIRED.value

MSG_NO_OPEN_PARENT = _(
    "That offer is no longer on the table — counter the current terms instead."
)
MSG_DRAFT_ONLY = _("Offers can only be changed while they are still a draft.")
MSG_ONLY_DRAFTER_SENDS = _("Only the offer's maker can send or edit it.")
MSG_ONLY_RECIPIENT = _(
    "Only the member the offer was made to can accept or counter it."
)
MSG_ONLY_PARTIES = _("Only the two members in this negotiation can do that.")
MSG_EMPTY_ITEM = _("Name the item — 'what', as offered.")
MSG_TOO_MANY_ITEMS = _(
    "A side can list at most %(n)d items — keep the offer focused."
)
MSG_TERMINAL_TIP = _(
    "This negotiation is closed at %(state)s — nothing left to decide on it."
)
MSG_EMPTY_SIDE = _(
    "A barter (or cash-plus) deal needs something on BOTH sides — list "
    "what you give and what you want."
)

# Sentinel: "no cash_amount value arrived" (None would be a legal value —
# a BARTER mode has NO cash, and CASH must have one; the caller's intent
# differs from both).
UNSET = object()


class ListingUnavailable(Exception):
    """A listing reference cannot be resolved through listings' PUBLIC
    door (absent while siblings build, or a draft/removed/missing listing).

    WHY a dedicated exception: callers render the honest 'listing not
    available' page and NEVER invent an owner to negotiate with.
    """

    def __init__(self, message=None):
        from django.utils.translation import gettext as _t  # runtime text

        self.message = _t(message) if message else _t(
            "That listing isn't available for offers."
        )
        super().__init__(self.message)


# ----------------------------------------------------------------- reads


def lineage_trail(row: Offer) -> list[Offer]:
    """The supersedence trail root → current terms, for the detail page:
    SENT rows along the parent-links path, newest sent child wins at each
    step; an unsent DRAFT `row` is appended in place (its composer's page
    shows its position in the trail). Cycle-guarded with the models' caps.

    WHY a walk (not one queryset): per-row `parent` links + the
    newest-sent-child rule is the SUPEDERSENCE ORDER definition — one
    place, one consumer shape (view trail, tip logic, sweep, Prompt 22).
    """
    node = row.walk_to_root()
    trail: list[Offer] = []
    if node.is_sent:
        trail.append(node)
    for _ in range(100):
        sent_children = [child for child in node.children.all() if child.is_sent]
        if not sent_children:
            break
        node = max(sent_children, key=lambda child: (child.created_at, child.pk or 0))
        trail.append(node)
    if not row.is_sent and row.pk is not None and (not trail or trail[-1].pk != row.pk):
        trail.append(row)  # an unsent draft renders in place, under its parent
    return trail


def tip_of(row: Offer) -> Offer | None:
    """The CURRENT terms of the lineage `row` belongs to — the last SENT
    row of its supersedence path (private drafts never mask live terms).

    Returns None for an all-draft lineage (nothing on the table yet).
    """
    node = row.walk_to_root()
    tip = node if node.is_sent else None
    for _ in range(100):
        sent_children = [child for child in node.children.all() if child.is_sent]
        if not sent_children:
            break
        node = max(sent_children, key=lambda child: (child.created_at, child.pk or 0))
        tip = node
    return tip


def get_offer_for(user, offer_id: int) -> Offer | None:
    """Membership-gated single-offer read; None == 404 in the view.

    IDOR shape (messaging precedent, verbatim policy): a non-party sees
    the SAME answer as a missing row — a distinguishable error would let
    an ID-guesser confirm which offers exist. DRAFT rows are visible ONLY
    to their composer (a draft is one member's private workspace).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        row = Offer.objects.select_related("initiator", "counterpart", "parent").get(
            pk=offer_id
        )
    except Offer.DoesNotExist:
        return None
    if row.status == DRAFT and row.initiator_id != user.pk:
        return None  # drafts: the composer's eyes only
    if row.status != DRAFT and not row.is_party(user):
        return None  # == 404 to the guesser (message-missing equivalence)
    return row


# ------------------------------------------------ sibling-listing resolution


def resolve_listing_offer(listing_id) -> tuple:
    """Read the sibling listing through its PUBLIC surface (Prompt 17's
    get_visible_listing). Returns (owner_user, title_or_None); raises
    ListingUnavailable when it can't resolve — the view renders the honest
    page, never an invented counterparty.

    WHY for_user=None: an offer may only open against a PUBLISHED listing
    — someone else's draft/review/removal must refuse (the caller shows a
    human-readable refusal), never leak an owner by side effect.
    """
    from apps.listings.services import get_visible_listing

    try:
        listing = get_visible_listing(int(listing_id), None)
    except (TypeError, ValueError):
        raise ListingUnavailable(_("That listing reference is not valid."))
    if listing is None or getattr(listing.seller, "pk", None) is None:
        raise ListingUnavailable()
    title = str(listing.title or "").strip() or None
    return listing.seller, title


# ---------------------------------------------------------- write helpers


def _clean_items_payload(items) -> list[tuple[str, str]]:
    """Normalize [(item, qty), ...] into clean tuples; refuse bad rows.

    Empty slots (a formset always posts a few blanks) are dropped;
    qty-without-item refuses (a quantity of nothing is a typo, not a
    term). WHY here and not only in the form: services are the door EVERY
    caller uses — API scripts included.
    """
    payload: list[tuple[str, str]] = []
    for entry in items or ():
        seq = entry if isinstance(entry, (tuple, list)) else (entry, "")
        first = seq[0] if len(seq) > 0 else ""
        second = seq[1] if len(seq) > 1 else ""
        item_text = str(first or "").strip()
        qty_text = str(second or "").strip()
        if not item_text and not qty_text:
            continue  # a fully blank slot is nothing offered
        if not item_text:
            raise ValidationError(MSG_EMPTY_ITEM)
        if len(payload) >= MAX_ITEMS_PER_SIDE:
            raise ValidationError(MSG_TOO_MANY_ITEMS % {"n": MAX_ITEMS_PER_SIDE})
        payload.append((item_text, qty_text))
    return payload


def _save_side_items(offer: Offer, side: str, payload) -> int:
    """Replace ONE side's rows atomically (inside create/update calls)."""
    OfferItem.objects.filter(offer=offer, side=side).delete()
    for item_text, qty_text in payload:
        row = OfferItem(
            offer=offer, side=side, item=item_text, qty=qty_text
        )
        row.full_clean()  # caps + "name the item" at model level too
        row.save()
    return len(payload)


def _assert_mode_cash(mode: str, cash_amount) -> str:
    """The MODEL's mode/cash rule, restated for services' own calls.

    NOTE: this is the same pair of reasons the Offer.clean() raises (one
    copy in models.py, imported here) — service callers get the friendly
    refusal BEFORE a partial save instead of a DB constraint error.
    """
    try:
        mode_value = BarterMode(mode).value
    except ValueError as exc:
        raise ValidationError(MSG_BAD_MODE % {"mode": str(mode)}) from exc
    if mode_value in (BarterMode.CASH.value, BarterMode.CASH_PLUS.value) and (
        cash_amount is None
    ):
        raise ValidationError(MSG_CASH_REQUIRED % {"mode": mode_value})
    if mode_value == BarterMode.BARTER.value and cash_amount is not None:
        raise ValidationError(MSG_CASH_FORBIDDEN)
    return mode_value


# ----------------------------------------------------------------- writes


@transaction.atomic
def create_offer(
    *,
    initiator,
    counterpart,
    mode: str,
    cash_amount=None,
    give_items=(),
    want_items=(),
    parent: Offer | None = None,
    origin_listing_id: int | None = None,
    now=None,
) -> Offer:
    """A member's FIRST terms as a DRAFT version row (+ item snapshots).

    Everything a compose page posted persists or nothing does — one
    atomic unit (offer row + both sides). The offer STAYS a draft until
    send_offer puts it on the table; DRAFT is where mode/cash/items stay
    editable (spec: 'items editable ONLY while the offer version is
    DRAFT').
    `counterpart` is a resolved User — the VIEW derives the listing owner
    through listings' PUBLIC door (resolve_listing_offer) before calling;
    `parent` (counters only) links the new version to the row it
    supersedes and is integrity-checked by Offer.clean (ping-pong);
    `now` injects the clock (deterministic sweeps/tests).
    """
    if counterpart is None or getattr(counterpart, "pk", None) is None:
        raise ValidationError(_("Pick the member you are dealing with."))
    if counterpart.pk == initiator.pk:
        raise ValidationError(MSG_SELF_DEAL)
    mode_value = _assert_mode_cash(mode, cash_amount)
    give = _clean_items_payload(give_items)
    want = _clean_items_payload(want_items)
    offer = Offer(
        initiator=initiator,
        counterpart=counterpart,
        parent=parent,
        mode=mode_value,
        cash_amount=cash_amount,
        origin_kind=(
            OfferOrigin.LISTING.value if origin_listing_id else OfferOrigin.DIRECT.value
        ),
        origin_listing_id=(int(origin_listing_id) if origin_listing_id else None),
        status=DRAFT,
        last_activity_at=(now or timezone.now()),
    )
    offer.full_clean()  # mode/cash pair + ping-pong + field caps, in words
    offer.save()
    _save_side_items(offer, OfferSide.GIVE.value, give)
    _save_side_items(offer, OfferSide.WANT.value, want)
    return offer


@transaction.atomic
def update_draft(
    offer: Offer,
    *,
    by_user,
    mode: str | None = None,
    cash_amount: Decimal | None | object = UNSET,
    give_items=None,
    want_items=None,
    now=None,
) -> Offer:
    """Edit a DRAFT version — mode/cash and EACH side independently.

    THE DRAFT-ONLY RULE IS THE EDITABILITY GATE (spec verbatim: side items
    'editable in DRAFT only'): once a version has left DRAFT the same
    wall refuses edits after OFFERED, after ACCEPTED (item/text/cash
    alike) — one gate, no special cases.
    """
    if offer.status != DRAFT:
        raise ValidationError(MSG_DRAFT_ONLY)
    if by_user is None or offer.initiator_id != getattr(by_user, "pk", None):
        # WHY initiator-only (drafts are private): the recipient sees no
        # draft at all (get_offer_for answers None); editing by anyone but
        # its maker is the same refusal.
        raise ValidationError(MSG_ONLY_DRAFTER_SENDS)
    if mode is not None:
        offer.mode = mode
    if cash_amount is not UNSET:
        offer.cash_amount = (
            None if cash_amount in (None, "") else Decimal(str(cash_amount))
        )
    # Re-affirm the pair AFTER both may have changed together, then persist.
    _assert_mode_cash(offer.mode, offer.cash_amount)
    offer.full_clean()
    offer.save(update_fields=["mode", "cash_amount", "updated_at"])
    if give_items is not None:
        _save_side_items(offer, OfferSide.GIVE.value, _clean_items_payload(give_items))
    if want_items is not None:
        _save_side_items(offer, OfferSide.WANT.value, _clean_items_payload(want_items))
    return offer


@transaction.atomic
def send_offer(offer: Offer, *, by_user, now=None) -> Offer:
    """Put a draft on the table: DRAFT → OFFERED — and, for counters, the
    superseded parent OFFERED → COUNTERED — atomically.

    ROLE RULE (services layer): only the version's maker sends it. The
    parent re-check keeps the lineage honest under races: by the time a
    counter is SENT, its parent must STILL be OFFERED (accepted /
    countered / cancelled / expired meanwhile → refused, citing that the
    terms moved on — never fabricated state). A BARTER/CASH_PLUS draft
    must ALSO carry something on BOTH sides (the money-only CASH mode is
    free of item requirements): drafts are editing workspaces and may
    transiently miss a side, but nothing half-worded goes out.
    """
    if offer.status != DRAFT:
        raise OfferStatusError(MSG_DRAFT_ONLY)
    if offer.initiator_id != getattr(by_user, "pk", None):
        raise ValidationError(MSG_ONLY_DRAFTER_SENDS)
    _assert_coherent_terms(offer)
    parent = offer.parent if offer.parent_id else None
    if parent is not None:
        parent.refresh_from_db(fields=["status"])
        if parent.status != OFFERED:
            raise ValidationError(MSG_NO_OPEN_PARENT)
    offer.transition(OFFERED, by_user=by_user, now=now)
    if parent is not None:
        parent.transition(COUNTERED, by_user=by_user, now=now)
    return offer


def _assert_coherent_terms(offer: Offer) -> None:
    """The send-time term-consistency gate (see send_offer docstring)."""
    mode_value = BarterMode(offer.mode).value
    if mode_value not in (BarterMode.BARTER.value, BarterMode.CASH_PLUS.value):
        return  # CASH deals are the stated amount — items optional
    give_n = offer.items.filter(side=OfferSide.GIVE.value).count()
    want_n = offer.items.filter(side=OfferSide.WANT.value).count()
    if not give_n or not want_n:
        raise ValidationError(MSG_EMPTY_SIDE)


@transaction.atomic
def create_counteroffer(
    *,
    parent: Offer,
    counterer,
    mode: str,
    cash_amount=None,
    give_items=(),
    want_items=(),
    now=None,
) -> Offer:
    """The COUNTER terms as a NEW version row (a DRAFT linked to `parent`).

    WHY a new row per counter (never mutating the original): versions are
    the audit trail — every stage's terms stay inspectable forever, and
    lineages walk parent links to the root. The counterer must be the
    parent's CURRENT recipient (role rule) and the parent still open
    (OFFERED) — countering after acceptance/cancel/counter/expiry is a
    refusal. Re-composing applies the new content to the composer's
    existing draft child (idempotent compose, mirroring messaging's
    enquiry idempotency) — one draft per composer per open parent.
    """
    if counterer is None or getattr(counterer, "pk", None) is None:
        raise ValidationError(MSG_ONLY_RECIPIENT)
    if counterer.pk != parent.counterpart_id:
        raise ValidationError(MSG_ONLY_RECIPIENT)
    if parent.status != OFFERED:
        raise ValidationError(MSG_NO_OPEN_PARENT)
    existing = (
        Offer.objects.filter(parent=parent, initiator=counterer, status=DRAFT)
        .first()
    )
    if existing is not None:
        return update_draft(
            existing,
            by_user=counterer,
            mode=mode,
            cash_amount=cash_amount,
            give_items=give_items,
            want_items=want_items,
            now=now,
        )
    return create_offer(
        initiator=counterer,
        counterpart=parent.initiator,
        parent=parent,
        mode=mode,
        cash_amount=cash_amount,
        give_items=give_items,
        want_items=want_items,
        # same origin lineage as the parent (the negotiation's reason)
        origin_listing_id=(
            parent.origin_listing_id
            if parent.origin_kind == OfferOrigin.LISTING.value
            else None
        ),
        now=now,
    )


@transaction.atomic
def accept(offer: Offer, *, by_user, now=None) -> Offer:
    """Accept the lineage's CURRENT terms: tip OFFERED → ACCEPTED, then the
    settlement propagates up the chain (superseded ancestors COUNTERED →
    ACCEPTED, so the whole trail reads agreed at one deal — the root row
    carries the final state for the settlement queries).

    ROLE RULE: only the member the tip was made to accepts it; accepting
    your own offer is nothing. Double-accept is refused by the state
    machine itself (an ACCEPTED tip has no ACCEPTED out-edge — the model
    raises with 'This offer is already accepted.').
    """
    tip = tip_of(offer)
    if tip is None or tip.status != OFFERED:
        raise OfferStatusError(MSG_TERMINAL_TIP % {"state": tip.status if tip else DRAFT})
    if tip.counterpart_id != getattr(by_user, "pk", None):
        raise ValidationError(MSG_ONLY_RECIPIENT)
    tip.transition(ACCEPTED, by_user=by_user, now=now)
    _propagate(tip, ACCEPTED, by_user=by_user, now=now)
    return tip


@transaction.atomic
def complete(offer: Offer, *, by_user, now=None) -> Offer:
    """The handoff happened: tip ACCEPTED → COMPLETED by EITHER party
    (the first member to mark it closes it; the audit trail keeps WHO).

    Fires the deal_completed signal (services docstring — the Prompt 22
    hook). Fired synchronously inside this transaction ON PURPOSE: pytest
    observes it, V1 listeners are in-process; listeners must not raise
    into the deal flow (documented on signals.deal_completed).
    """
    tip = tip_of(offer)
    if tip is None or tip.status != ACCEPTED:
        raise OfferStatusError(MSG_TERMINAL_TIP % {"state": tip.status if tip else DRAFT})
    if not tip.is_party(by_user):
        raise ValidationError(MSG_ONLY_PARTIES)
    tip.transition(COMPLETED, by_user=by_user, now=now)
    _propagate(tip, COMPLETED, by_user=by_user, now=now)
    signals.deal_completed.send(
        sender=Offer,
        offer=tip,
        root=tip.walk_to_root(),
        parties=(tip.initiator_id, tip.counterpart_id),
    )
    return tip


@transaction.atomic
def cancel(offer: Offer, *, by_user, now=None) -> Offer:
    """Stop the negotiation: tip → CANCELLED by EITHER party, propagated
    up the chain so the whole lineage reads closed (a half-alive record
    would mislead both members; a NEW negotiation starts a NEW lineage).

    Terminal rows stay closed — the state machine refuses anything later.
    """
    tip = tip_of(offer)
    if tip is None or tip.status not in (OFFERED, COUNTERED, ACCEPTED):
        raise OfferStatusError(MSG_TERMINAL_TIP % {"state": tip.status if tip else DRAFT})
    if not tip.is_party(by_user):
        raise ValidationError(MSG_ONLY_PARTIES)
    tip.transition(CANCELLED, by_user=by_user, now=now)
    _propagate(tip, CANCELLED, by_user=by_user, now=now)
    return tip


def _propagate(tip: Offer, to_status: str, *, by_user, now=None) -> int:
    """Settle the supersedence chain behind the tip to `to_status`.

    WHY propagation (design note): a lineage settles as ONE deal — the
    trail must never read 'agreed at the tip, parent still countered'.
    Every non-draft, non-terminal ancestor moves (the model dict blesses
    exactly these edges: COUNTERED→ACCEPTED/CANCELLED/EXPIRED and
    ACCEPTED→COMPLETED/CANCELLED). DRAFT junk — a counter composed but
    never sent — is left as the private workspace it is.
    """
    moved = 0
    for node in tip.lineage()[:-1]:
        if node.status == DRAFT or node.is_terminal or node.status == to_status:
            continue
        node.transition(to_status, by_user=by_user, now=now)
        moved += 1
    return moved


# ------------------------------------------------------------------ sweep


def expire_stale_offers(*, now=None) -> int:
    """The 7-day inactivity sweep (cron/admin hook; service + its tests).

    Open lineage TIPS (OFFERED — and COUNTERED, the superseded-ancestor
    state the sweep also honours at the tip position) whose last
    negotiation activity is older than EXPIRY_DAYS expire: tip → EXPIRED
    (by_user=None — the sweep's signature; the model gates None to EXPIRED
    moves only), ancestors propagated → EXPIRED, so a dead lineage reads
    closed END-to-end. Returns the NUMBER OF ROWS moved (tips + their
    propagated ancestors).

    Boundary contract: at EXACTLY EXPIRY_DAYS elapsed the offer expires
    (`activity <= now - window`); tests pin the boundary with an injected
    clock. WHY tips only: superseded rows are history — freezing the tip
    is closing THE negotiation; history rows move only via propagation.
    """
    now = now or timezone.now()
    window = timedelta(days=EXPIRY_DAYS)
    expired = 0
    open_pks = list(
        Offer.objects.filter(status__in=[OFFERED, COUNTERED]).values_list("pk", flat=True)
    )
    for row in Offer.objects.filter(pk__in=open_pks):
        the_tip = tip_of(row)
        if the_tip is None or the_tip.status not in (OFFERED, COUNTERED):
            continue
        activity = the_tip.last_activity_at or the_tip.created_at
        if activity is not None and activity <= now - window:
            the_tip.transition(EXPIRED, by_user=None, now=now)
            expired += 1
            expired += _propagate(the_tip, EXPIRED, by_user=None, now=now)
    return expired


# ------------------------------------------------------- public queries


def completed_deals_between(user_a, user_b):
    """Root rows the two members settled (either direction), oldest first.

    Prompt 22's rollup reads these; each root's parties are exactly the
    two users (the ping-pong invariant). The root carries the settlement —
    propagation keeps it in step with the tip.
    """
    return (
        Offer.objects.filter(parent__isnull=True, status=COMPLETED)
        .filter(
            Q(initiator=user_a, counterpart=user_b)
            | Q(initiator=user_b, counterpart=user_a)
        )
        .order_by("created_at")
    )


def completed_deals_for(user):
    """Root rows the member settled (any counterpart), newest first."""
    return (
        Offer.objects.filter(parent__isnull=True, status=COMPLETED)
        .filter(Q(initiator=user) | Q(counterpart=user))
        .order_by("-updated_at")
    )


def open_negotiations_for(user):
    """Lineage roots whose CURRENT state is live — waiting (OFFERED /
    propagated-COUNTERED) or agreed-not-yet-done (ACCEPTED). Newest first.
    Drafts excluded: private workspaces (my_drafts serves those)."""
    return (
        Offer.objects.filter(parent__isnull=True)
        .filter(Q(initiator=user) | Q(counterpart=user))
        .filter(status__in=[OFFERED, COUNTERED, ACCEPTED])
        .select_related("initiator", "counterpart")
        .order_by("-updated_at")
    )


def my_drafts(user):
    """The member's own unsent versions — fully editable, never seen by
    the other party until sent."""
    if not getattr(user, "pk", None):
        return Offer.objects.none()
    return Offer.objects.filter(initiator=user, status=DRAFT).order_by("-updated_at")


def actions_for(row: Offer, user) -> dict:
    """CTA flags for the template — the RIGHT side user at the RIGHT state.

    WHY a service helper (not view code): ONE policy decides both what
    renders (detail page) and what the server accepts (POST routes call
    the same services — no divergent checks).

    Map (role → move; tip status first):
    - DRAFT (maker only): edit + send.
    - OFFERED tip: recipient may accept/counter; either party may cancel
      (initiator retracting a silent offer == recipient declining: both
      cancel — one honest terminal word for 'I'm walking away').
    - ACCEPTED tip: either party completes (the handoff happened) or
      cancels (it fell through).
    """
    actions = {
        "can_edit": False,
        "can_send": False,
        "can_accept": False,
        "can_counter": False,
        "can_cancel": False,
        "can_complete": False,
        "tip_pk": None,
        "tip_status": None,
    }
    if user is None or not getattr(user, "is_authenticated", False):
        return actions
    if row.status == DRAFT:
        if row.initiator_id == user.pk:
            actions["can_edit"] = True
            actions["can_send"] = True
        return actions
    tip = tip_of(row)
    if tip is None:
        return actions
    actions["tip_pk"] = tip.pk
    actions["tip_status"] = tip.status
    is_counterpart = tip.counterpart_id == user.pk
    if tip.status == OFFERED:
        if is_counterpart:
            actions["can_accept"] = True
            actions["can_counter"] = True
        actions["can_cancel"] = True
    elif tip.status == ACCEPTED:
        actions["can_complete"] = True
        actions["can_cancel"] = True
    return actions


# ---------------------------------------------------------------------------
# Prompt 22 public doors (reviews' origin contract — added at consolidation).
#


def _root_row(deal_id) -> Offer | None:
    """The ROOT offer for a reviews deal_id, or None. reviews callers may
    pass a root id OR any version id — lineage resolves first (honest None
    for missing/invalid ids, never a fabricated deal)."""
    try:
        row = Offer.objects.get(pk=int(deal_id))
    except (Offer.DoesNotExist, TypeError, ValueError):
        return None
    try:
        return row.walk_to_root()
    except ValidationError:
        return None


COMPLETED = OfferStatus.COMPLETED.value


def review_for(deal_id, reviewer_id) -> int | None:
    """reviews' door: the member `reviewer_id` should review for barter deal
    `deal_id`, or None (not a party / deal not reviewable).

    WHY root + COMPLETED only: reviews are MUTUAL AFTER a settled deal
    (PROMPTS.md Prompt 22) — a live negotiation is not a reviewable deal.
    The reviewer always reviews the OTHER party (their own id is never
    returned; reviews refuses self-reviews independently).
    """
    root = _root_row(deal_id)
    if root is None or root.status != COMPLETED:
        return None
    if reviewer_id == root.initiator_id:
        return root.counterpart_id
    if reviewer_id == root.counterpart_id:
        return root.initiator_id
    return None


def deal_label(deal_id) -> str | None:
    """reviews' honest deal title: first GIVE ⇄ first WANT item of the root
    version, or None when the deal is gone (fallback 'Deal #<id>' applies)."""
    root = _root_row(deal_id)
    if root is None:
        return None
    items = list(root.items.values_list("side", "item"))
    give = next((i for s, i in items if s == OfferSide.GIVE.value), "")
    want = next((i for s, i in items if s == OfferSide.WANT.value), "")
    if give and want:
        return "%s ⇄ %s" % (give, want)
    return "Barter offer #%d" % root.pk
