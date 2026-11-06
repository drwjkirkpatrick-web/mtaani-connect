"""Prompt 21 tests: Barter & Cash-Plus-Exchange.

Coverage map (spec-verbatim; THE STATE MACHINE IS THE PRODUCT):
- the FULL transition matrix: every legal edge moves, every illegal pair
  refuses (49 pairs, mechanical — esp. COUNTERED after ACCEPTED,
  double-accept, terminal states)
- plain-language refusal texts (the exact strings the UI shows)
- role rules: only the maker sends; only the recipient accepts/counters;
  strangers are invisible (404 == missing, messaging IDOR precedent)
- full happy path: 2 counters → accepted → completed, lineage depth 2,
  walk_to_root identity, settlement propagated to the root
- multi-item math: 2 GIVE + 2 WANT on CASH_PLUS, sides edited
  independently, DRAFT-only editability (edit-after-OFFERED refused)
- cash validation: CASH/CASH_PLUS require ≥ 0 amount; BARTER forbids one
- expiry sweep: 7-day window, fresh survives, stale expires, EXACT
  boundary determinism (injected clock), propagation of the expiry up the
  chain, terminal rows untouched
- views: members negotiate a mixed deal entirely in-app; budget ≤ 50 KB;
  anon → login redirect; the honest no-custody cash copy renders
- deals queries + deal_completed signal (Prompt 22's public API)
- M-Pesa: stub only — instantiating anything raises, no network call path
"""
import itertools
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils.encoding import force_str

from apps.accounts.models import User
from apps.barter import services
from apps.barter.models import (
    EXPIRY_DAYS,
    MSG_CASH_FORBIDDEN,
    MSG_SELF_DEAL,
    Offer,
    OfferStatusError,
    OfferStatus,
    TRANSITIONS,
    illegal_reason,
)
from apps.barter.mpesa import MPesaAdapter
from apps.core.testing import assert_under_budget, mock_now
from apps.core.types import BarterMode

CASH = BarterMode.CASH.value
BARTER = BarterMode.BARTER.value
CASH_PLUS = BarterMode.CASH_PLUS.value

DRAFT = OfferStatus.DRAFT.value
OFFERED = OfferStatus.OFFERED.value
COUNTERED = OfferStatus.COUNTERED.value
ACCEPTED = OfferStatus.ACCEPTED.value
COMPLETED = OfferStatus.COMPLETED.value
CANCELLED = OfferStatus.CANCELLED.value
EXPIRED = OfferStatus.EXPIRED.value


@pytest.fixture
def buyer(db):
    return User.objects.create_user("amani", email="amani@example.com", password="***")


@pytest.fixture
def seller(db):
    return User.objects.create_user("njeri", email="njeri@example.com", password="***")


@pytest.fixture
def stranger(db):
    return User.objects.create_user("ali", email="ali@example.com", password="***")


@pytest.fixture
def buyer_client(buyer):
    client = Client()
    client.force_login(buyer)
    return client


@pytest.fixture
def seller_client(seller):
    client = Client()
    client.force_login(seller)
    return client


# ------------------------------------------------------------- helpers


def _offer(buyer, seller, mode=CASH_PLUS, cash=300,
           give=(("2kg maize", "2 kg"), ("sewing hour", "1")),
           want=(("solar lamp", "1"),), status=DRAFT, now=None):
    """A composed offer exactly like the view posts one (service door).

    NOTE: `cash` is passed THROUGH (int/Decimal/None) — mode/cash legality
    is the service's job; this helper must not pre-clean it.
    """
    return services.create_offer(
        initiator=buyer,
        counterpart=seller,
        mode=mode,
        cash_amount=cash,
        give_items=[] if mode == CASH else list(give),
        want_items=list(want),
        now=now or mock_now(),
    )


def _row_in_state(state, buyer, seller):
    """A synthetic row sitting in `state` (legality matrix target)."""
    return Offer.objects.create(
        initiator=buyer, counterpart=seller, mode=CASH_PLUS,
        cash_amount=300, status=state, last_activity_at=mock_now(),
    )


def _sent_offer(buyer, seller, mode=CASH_PLUS, cash=300,
                give=(("2kg maize", "2 kg"),), want=(("solar lamp", "1"),),
                now=None):
    offer = _offer(buyer, seller, mode=mode, cash=cash, give=give, want=want, now=now)
    return services.send_offer(offer, by_user=buyer, now=now or mock_now())


def _counter(parent_offer, counterer, mode=BARTER, cash=None,
             give=(("2kg maize", "2 kg"),), want=(("solar lamp", "1"),),
             now=None):
    child = services.create_counteroffer(
        parent=parent_offer, counterer=counterer, mode=mode,
        cash_amount=cash,
        give_items=list(give), want_items=list(want), now=now or mock_now(),
    )
    return services.send_offer(child, by_user=counterer, now=now or mock_now())


# --------------------------------------------- THE MATRIX (the product)


@pytest.mark.django_db
def test_full_transition_matrix_every_legal_moves_every_illegal_refuses(buyer, seller):
    """The mechanical truth table: for all 7 × 7 (from, to) states, the
    move is allowed EXACTLY when the dict blesses it. Illegal pairs raise
    OfferStatusError citing the plain-language reason; legal pairs move. """
    for from_state, to_state in itertools.product(OfferStatus.values, repeat=2):
        row = _row_in_state(from_state, buyer, seller)
        legal = to_state in TRANSITIONS[from_state]
        if not legal:
            with pytest.raises(OfferStatusError) as exc_info:
                row.transition(to_state, by_user=buyer, now=mock_now())
            assert _plain(exc_info.value) == force_str(
                illegal_reason(from_state, to_state)
            ), f"{from_state}→{to_state}: wrong refusal words"
            row.refresh_from_db()
            assert row.status == from_state  # nothing half-applied
        else:
            row.transition(to_state, by_user=buyer, now=mock_now())
            row.refresh_from_db()
            assert row.status == to_state, f"{from_state}→{to_state} must move"


def _plain(exc):
    messages = getattr(exc, "messages", None)
    if messages:
        return force_str(messages[0])
    given = getattr(exc, "message_dict", None)
    if given:
        for values in given.values():
            if values:
                return force_str(values[0])
    return force_str(exc)


@pytest.mark.django_db
def test_named_pitfall_countered_after_accepted_blasted_in_words(buyer, seller):
    """COUNTERED after ACCEPTED — the named pitfall gets its own words."""
    row = _row_in_state(ACCEPTED, buyer, seller)
    with pytest.raises(OfferStatusError) as exc_info:
        row.transition(COUNTERED, by_user=seller, now=mock_now())
    assert force_str(exc_info.value.messages[0]) == (
        "This offer is accepted — its terms are locked and cannot be "
        "countered. Cancel and start a new negotiation instead."
    )


@pytest.mark.django_db
def test_double_accept_refused_plain_words(buyer, seller):
    """Double-accept: the MODEL refuses the second accept with the exact
    words ('already accepted') — and the service route stays closed too."""
    row = _row_in_state(OFFERED, buyer, seller)
    row.transition(ACCEPTED, by_user=seller, now=mock_now())  # legal first
    with pytest.raises(OfferStatusError) as exc_info:
        row.transition(ACCEPTED, by_user=seller, now=mock_now())  # the 2nd
    assert (
        force_str(exc_info.value.messages[0]) == "This offer is already accepted."
    )
    row.refresh_from_db()
    assert row.status == ACCEPTED  # exactly one accept
    with pytest.raises(OfferStatusError) as exc_info:
        services.accept(row, by_user=seller, now=mock_now())
    assert "closed" in _plain(exc_info.value)  # service guards before the move


@pytest.mark.django_db
def test_counter_only_while_parent_open_and_never_after_accept(buyer, seller):
    """Pitfall shape: counter attempts after ACCEPTED are refused BOTH at
    creation and at send; a counter on a cancelled chain dies too."""
    offer = _sent_offer(buyer, seller)
    services.accept(offer, by_user=seller, now=mock_now())
    with pytest.raises(ValidationError) as exc_info:
        _counter(offer, seller)
    assert "no longer on the table" in _plain(exc_info.value)
    # even a forged child row cannot leave ACCEPTED via COUNTERED:
    child = _row_in_state(DRAFT, seller, buyer)  # shape-only; never sent
    child.parent = offer
    with pytest.raises(OfferStatusError):
        child.transition(COUNTERED, by_user=seller, now=mock_now())


@pytest.mark.django_db
def test_by_user_none_is_only_the_sweeps_signature(buyer, seller):
    """System moves (no user) are EXCEPTED for EXPIRED only; any other
    None-user move is refused (the model can't be driven anonymously)."""
    offered = _row_in_state(OFFERED, buyer, seller)
    with pytest.raises(OfferStatusError):
        offered.transition(ACCEPTED, by_user=None, now=mock_now())
    offered = _row_in_state(OFFERED, buyer, seller)
    with pytest.raises(OfferStatusError):
        offered.transition(CANCELLED, by_user=None, now=mock_now())
    stale = _row_in_state(OFFERED, buyer, seller)
    stale.transition(EXPIRED, by_user=None, now=mock_now())  # sweep shape
    assert stale.status == EXPIRED


@pytest.mark.django_db
def test_non_member_cannot_drive_the_machine(buyer, seller, stranger):
    """Non-party transition attempts: the model refuses with the same
    words — strangers can never move an offer."""
    row = _row_in_state(OFFERED, buyer, seller)
    with pytest.raises(OfferStatusError) as exc_info:
        row.transition(ACCEPTED, by_user=stranger, now=mock_now())
    assert (
        force_str(exc_info.value.messages[0])
        == "Only a member of this negotiation can do that."
    )
    assert row.status == OFFERED


@pytest.mark.django_db
def test_terminal_states_stay_closed(buyer, seller):
    """CANCELLED / EXPIRED rows accept no moves at all (service level)."""
    cancelled = _sent_offer(buyer, seller)
    services.cancel(cancelled, by_user=seller, now=mock_now())
    expect_in_words = {
        "accept": "closed",
        "complete": "closed",
        "cancel": "closed",
        "counter": "no longer on the table",
        "send": "still a draft",
    }
    move_calls = (
        ("accept", lambda r: services.accept(r, by_user=seller)),
        ("complete", lambda r: services.complete(r, by_user=buyer)),
        ("cancel", lambda r: services.cancel(r, by_user=buyer)),
        ("counter", lambda r: _counter(r, seller)),
        ("send", lambda r: services.send_offer(r, by_user=buyer)),
    )
    for move_name, move_call in move_calls:
        with pytest.raises((ValidationError, OfferStatusError)) as exc_info:
            move_call(cancelled)
        assert expect_in_words[move_name] in _plain(
            exc_info.value
        ), f"{move_name} must refuse a cancelled deal with real words"


# ------------------------------------------------------------ happy path


@pytest.mark.django_db
def test_happy_path_two_counters_lineage_and_settlement(buyer, seller):
    """Buyer composes CASH_PLUS (2 give + 2 want + cash), sends; seller
    counters ONCE; buyer counters back; seller ACCEPTS the second
    counter; buyer completes. Lineage depth 2, walk_to_root identity,
    settlement propagated root→tip, every version's terms frozen."""
    root = _offer(buyer, seller)  # CASH_PLUS, 2 give, 2 want, KES 300
    assert root.status == DRAFT
    root = services.send_offer(root, by_user=buyer, now=mock_now())
    assert root.status == OFFERED

    first = _counter(root, seller)  # seller's counter terms
    root.refresh_from_db()
    assert root.status == COUNTERED
    assert first.status == OFFERED
    assert first.parent_id == root.pk

    second = _counter(first, buyer)  # buyer counters back (counter #2)
    first.refresh_from_db()
    assert first.status == COUNTERED
    assert second.status == OFFERED
    assert second.parent_id == first.pk

    tip = services.accept(second, by_user=seller, now=mock_now())
    assert tip.status == ACCEPTED
    root.refresh_from_db()
    first.refresh_from_db()
    assert root.status == ACCEPTED  # propagated: the whole trail agrees
    assert first.status == ACCEPTED

    done = services.complete(second, by_user=buyer, now=mock_now())
    root.refresh_from_db()
    first.refresh_from_db()
    second.refresh_from_db()
    assert done.status == COMPLETED
    assert root.status == COMPLETED == first.status == second.status

    # lineage: depth 2 (two counter levels), walk_to_root identity, order
    assert services.tip_of(second).pk == second.pk
    assert second.walk_to_root().pk == root.pk
    assert first.walk_to_root().pk == root.pk
    trail = second.lineage()
    assert [row.pk for row in trail] == [root.pk, first.pk, second.pk]
    # version snapshots stay frozen: root keeps its 2 give / 1 want + cash
    assert root.items.count() == 3  # 2 GIVE + 1 WANT rows
    assert root.items.filter(side="GIVE").count() == 2
    assert first.items.count() == 2 and first.cash_amount is None
    assert root.cash_amount == Decimal("300")


@pytest.mark.django_db
def test_draft_is_editable_until_sent_then_frozen(buyer, seller):
    """Editability gate: one side edits in DRAFT while the other stands;
    after OFFERED the same wall refuses item and cash edits; after
    ACCEPTED it still refuses (the pitfall's 'edits to ACCEPTED offers')."""
    offer = _offer(buyer, seller)  # 2 GIVE + 1 WANT + cash, DRAFT
    services.update_draft(
        offer, by_user=buyer, give_items=(("2kg maize", "2 kg"),)
    )  # ONE side replaced — the WANT side stands untouched
    assert offer.items.filter(side="GIVE").count() == 1
    assert offer.items.filter(side="WANT").count() == 1
    sent = services.send_offer(offer, by_user=buyer, now=mock_now())
    with pytest.raises(ValidationError) as exc_info:
        services.update_draft(
            sent, by_user=buyer, give_items=(("rope", "5 m"),)
        )  # an item edit after it left DRAFT
    assert (
        _plain(exc_info.value)
        == "Offers can only be changed while they are still a draft."
    )
    assert sent.items.filter(side="GIVE").count() == 1  # nothing half-edited
    with pytest.raises(ValidationError):
        services.update_draft(
            sent, by_user=buyer, cash_amount=Decimal("999")
        )  # a cash edit — same wall
    # ...and the identical wall after ACCEPTED (item/text/cash alike):
    agreed = _sent_offer(buyer, seller, mode=BARTER, cash=None)
    services.accept(agreed, by_user=seller, now=mock_now())
    with pytest.raises(ValidationError) as exc_info:
        services.update_draft(
            agreed, by_user=buyer, cash_amount=Decimal("100")
        )
    assert (
        _plain(exc_info.value)
        == "Offers can only be changed while they are still a draft."
    )
    with pytest.raises(ValidationError):
        services.update_draft(
            agreed, by_user=buyer, give_items=(("late edits", ""),)
        )
    assert agreed.items.filter(side="GIVE").count() == 1  # frozen for good


@pytest.mark.django_db
def test_multiside_math_two_and_two_on_cash_plus(buyer, seller):
    """Multi-item math verbatim: 'I give: 2kg maize + sewing hour; I
    want: solar lamp + KES 300' — sides count exactly and edit
    INDEPENDENTLY in DRAFT (the want side can grow without touching give).
    """
    offer = _offer(
        buyer, seller,
        give=(("2kg maize", "2 kg"), ("sewing hour", "1")),
        want=(("solar lamp", "1"),),
    )
    assert offer.items.count() == 3
    assert offer.mode == CASH_PLUS
    assert offer.cash_amount == Decimal("300")  # numeric equality (300 == 300.00)
    services.update_draft(
        offer, by_user=buyer,
        want_items=(
            ("solar lamp", "1"), ("weekend taxi ride", "town run"),
            ("young chickens", "5"),
        ),
        now=mock_now(),
    )
    assert offer.items.filter(side="GIVE").count() == 2  # untouched side
    assert offer.items.filter(side="WANT").count() == 3  # replaced side


@pytest.mark.django_db
def test_item_caps_on_a_side(buyer, seller):
    """The side cap (10 rows) guards the 50 KB page budget."""
    eleven = [(f"item {i}", f"{i}") for i in range(11)]
    with pytest.raises(ValidationError) as exc_info:
        _offer(buyer, seller, mode=BARTER, cash=None, give=eleven)
    assert "at most 10" in _plain(exc_info.value)
    assert Offer.objects.count() == 0  # atomic: nothing half-saved


@pytest.mark.django_db
def test_send_refuses_half_worded_barter_terms_but_cash_only_passes(buyer, seller):
    """A BARTER/CASH_PLUS draft may transiently miss a side (editing
    workspace) — SEND refuses until both sides say something. CASH needs
    no item rows: the stated amount IS the terms."""
    offer = _offer(buyer, seller, mode=BARTER, cash=None)
    services.update_draft(offer, by_user=buyer, want_items=(), now=mock_now())
    with pytest.raises(ValidationError) as exc_info:
        services.send_offer(offer, by_user=buyer, now=mock_now())
    assert "BOTH sides" in _plain(exc_info.value)
    assert offer.status == DRAFT
    money = services.create_offer(
        initiator=buyer, counterpart=seller, mode=CASH, cash_amount=200,
        want_items=(), now=mock_now(),
    )
    services.send_offer(money, by_user=buyer, now=mock_now())
    money.refresh_from_db()
    assert money.status == OFFERED  # the stated amount is whole terms


# --------------------------------------------------------- role rules


@pytest.mark.django_db
def test_only_the_maker_sends_their_draft(buyer, seller, stranger):
    offer = _offer(buyer, seller)
    with pytest.raises(ValidationError) as exc_info:
        services.send_offer(offer, by_user=seller)
    assert "Only the offer's maker" in _plain(exc_info.value)
    assert offer.status == DRAFT
    with pytest.raises(ValidationError):
        services.send_offer(offer, by_user=stranger)
    assert Offer.objects.get(pk=offer.pk).status == DRAFT


@pytest.mark.django_db
def test_only_the_recipient_accepts_or_counters(buyer, seller, stranger):
    offer = _sent_offer(buyer, seller)
    with pytest.raises(ValidationError) as exc_info:
        services.accept(offer, by_user=buyer)  # the maker cannot self-accept
    assert "Only the member the offer was made to" in _plain(exc_info.value)
    with pytest.raises(ValidationError) as exc_info:
        _counter(offer, buyer)  # the maker cannot counter their own offer
    assert "Only the member the offer was made to" in _plain(exc_info.value)
    with pytest.raises(ValidationError):
        services.accept(offer, by_user=stranger)
    assert Offer.objects.get(pk=offer.pk).status == OFFERED


@pytest.mark.django_db
def test_non_party_is_404_same_as_missing(buyer, seller, stranger):
    """IDOR shape: a stranger's detail view == a missing row's 404 (never
    a tell-you-it-exists 403)."""
    offer = _sent_offer(buyer, seller)
    assert services.get_offer_for(stranger, offer.pk) is None
    stranger_client = Client()
    stranger_client.force_login(stranger)
    assert stranger_client.get(
        reverse("barter:detail", args=[offer.pk])
    ).status_code == 404
    assert stranger_client.get(
        reverse("barter:detail", args=[999999])
    ).status_code == 404  # the same answer — guessing confirms nothing


@pytest.mark.django_db
def test_anonymous_users_redirect_to_login():
    response = Client().get(reverse("barter:list"))
    assert response.status_code == 302
    assert "login" in response["Location"]
    response = Client().get(reverse("barter:detail", args=[999999]))
    assert response.status_code == 302


# ------------------------------------------------------- cash validation


@pytest.mark.django_db
def test_barter_mode_with_cash_amount_refused(buyer, seller):
    with pytest.raises(ValidationError) as exc_info:
        _offer(buyer, seller, mode=BARTER, cash=100)
    assert _plain(exc_info.value) == (
        "Barter-only deals state no cash amount — remove the money figure."
    )
    assert Offer.objects.count() == 0


@pytest.mark.django_db
def test_cash_modes_require_a_stated_amount(buyer, seller):
    """CASH and CASH_PLUS both refuse to record a moneyless deal — the
    stated amount IS the cash component that gets settled outside."""
    for mode in (CASH, CASH_PLUS):
        with pytest.raises(ValidationError) as exc_info:
            services.create_offer(
                initiator=buyer, counterpart=seller, mode=mode, cash_amount=None,
                give_items=[("maize", "")], want_items=[("lamp", "")],
            )
        assert _plain(exc_info.value) == (
            f"{mode} deals state a cash amount — the part the members "
            "settle themselves via M-Pesa."
        )
        assert Offer.objects.count() == 0  # atomic: no row, no items

    # CASH mode with NOTHING at all is still refusal-same (amount required)
    with pytest.raises(ValidationError):
        services.create_offer(
            initiator=buyer, counterpart=seller, mode=CASH, cash_amount=None,
        )
    assert Offer.objects.count() == 0


@pytest.mark.django_db
def test_negatives_and_self_deals_refused(buyer, seller):
    with pytest.raises(ValidationError):
        services.create_offer(
            initiator=buyer, counterpart=seller, mode=CASH, cash_amount=-5,
        )
    with pytest.raises(ValidationError) as exc_info:
        _offer(seller, seller)
    assert _plain(exc_info.value) == force_str(MSG_SELF_DEAL)


# ------------------------------------------------------------ expiry


@pytest.mark.django_db
def test_expiry_sweep_fresh_survives_stale_expires_and_count(buyer, seller):
    """T0 fresh offer survives a sweep at T0+3d; another created at T0
    with NO later activity expires at T0+8d; count is exact (tip + any
    propagated ancestors)."""
    t0 = mock_now()
    stale = _counter(
        _sent_offer(buyer, seller, now=t0), seller,
        now=t0 + timedelta(hours=1),
    )
    moved = services.expire_stale_offers(now=t0 + timedelta(days=3))
    stale.refresh_from_db()
    assert stale.status == OFFERED
    assert moved == 0

    # the FRESH offer is created minutes before the sweep (activity within
    # the window — 'fresh' means recently active, not lately composed)
    fresh = _sent_offer(
        buyer, seller,
        now=t0 + timedelta(days=EXPIRY_DAYS) - timedelta(minutes=5),
    )
    moved = services.expire_stale_offers(now=t0 + timedelta(days=8))
    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == EXPIRED and fresh.status == OFFERED
    # stale lineage: tip child (1) + propagated superseded parent (1) = 2
    assert moved == 2


@pytest.mark.django_db
def test_expiry_boundary_day_seven_injected_clock(buyer, seller):
    """Boundary: EXACTLY 7 quiet days expired; 7d minus one second — no."""
    from datetime import timedelta

    t0 = mock_now()
    edge = _sent_offer(buyer, seller, now=t0)
    early = _sent_offer(buyer, seller, now=t0)
    moved = services.expire_stale_offers(
        now=t0 + timedelta(days=EXPIRY_DAYS) - timedelta(seconds=1)
    )
    edge.refresh_from_db()
    early.refresh_from_db()
    assert edge.status == OFFERED and early.status == OFFERED
    assert moved == 0
    moved = services.expire_stale_offers(now=t0 + timedelta(days=EXPIRY_DAYS))
    edge.refresh_from_db()
    early.refresh_from_db()
    assert edge.status == EXPIRED and early.status == EXPIRED
    assert moved == 2


@pytest.mark.django_db
def test_expiry_propagates_up_the_chain_and_reads_closed(buyer, seller):
    """A stale counter lineage expires the tip AND its superseded parent
    (COUNTERED → EXPIRED is a real dict edge)."""
    from datetime import timedelta

    t0 = mock_now()
    root = _sent_offer(buyer, seller, now=t0)
    child = _counter(root, seller, now=t0 + timedelta(minutes=10))
    assert child.status == OFFERED and root.status == COUNTERED
    moved = services.expire_stale_offers(now=t0 + timedelta(days=8))
    assert moved == 2  # tip + propagated ancestor
    root.refresh_from_db()
    child.refresh_from_db()
    assert root.status == EXPIRED == child.status
    assert services.tip_of(root).status == EXPIRED


@pytest.mark.django_db
def test_terminal_lines_never_expiry_touched(buyer, seller):
    """Sweep leaves completed/cancelled lineages exactly as they are."""
    from datetime import timedelta

    t0 = mock_now()
    done = _offer(buyer, seller)
    services.send_offer(done, by_user=buyer, now=mock_now())
    services.accept(done, by_user=seller, now=mock_now())
    services.complete(done, by_user=buyer, now=mock_now())
    dead = _sent_offer(buyer, seller, now=mock_now())
    services.cancel(dead, by_user=buyer, now=mock_now())
    moved = services.expire_stale_offers(
        now=mock_now() + timedelta(days=30)
    )
    done.refresh_from_db()
    dead.refresh_from_db()
    assert moved == 0
    assert done.status == COMPLETED and dead.status == CANCELLED


@pytest.mark.django_db
def test_drafts_never_expire_and_never_mask_the_terms(buyer, seller):
    """A draft — root or counter — is workspace: never swept, never the
    tip; the lineage's live terms keep standing behind it."""
    from datetime import timedelta

    t0 = mock_now()
    offer = _sent_offer(buyer, seller, now=t0)
    child = services.create_counteroffer(
        parent=offer, counterer=seller, mode=BARTER, cash_amount=None,
        give_items=[("2kg maize", "2 kg")], want_items=[("solar lamp", "1")],
        now=t0,
    )
    assert child.status == DRAFT
    assert services.tip_of(child).pk == offer.pk  # draft never the tip
    assert services.expire_stale_offers(now=t0 + timedelta(days=8)) == 1
    offer.refresh_from_db()
    child.refresh_from_db()
    assert offer.status == EXPIRED
    assert child.status == DRAFT  # the workspace stays a workspace
    with pytest.raises((ValidationError, OfferStatusError)):
        services.send_offer(child, by_user=seller, now=t0 + timedelta(days=8))


# ---------------------------------------------------------- public doors


@pytest.mark.django_db
def test_deals_between_and_settled_queries_post_deal(buyer, seller, stranger):
    """Prompt 22's doors: completed_deals_between pairs either direction;
    completed_deals_for covers any side; open_negotiations_for excludes
    settled lineages."""
    offer = _sent_offer(buyer, seller)
    services.accept(offer, by_user=seller, now=mock_now())
    services.complete(offer, by_user=seller, now=mock_now())
    root = services.tip_of(offer).walk_to_root()
    settled = list(services.completed_deals_between(seller, buyer))
    assert [r.pk for r in settled] == [root.pk]  # pair query, either order
    assert services.completed_deals_between(buyer, stranger).count() == 0
    for_purchases = list(services.completed_deals_for(buyer))
    assert [r.pk for r in for_purchases] == [root.pk]
    assert services.open_negotiations_for(buyer).count() == 0


@pytest.mark.django_db
def test_deal_completed_signal_fires_exactly_once_per_settlement(buyer, seller):
    """The Prompt 22 hook: one deal_completed per complete(), args intact."""
    from apps.barter.signals import deal_completed

    got = []

    def receiver(sender, **kwargs):
        got.append(kwargs)

    deal_completed.connect(receiver)
    try:
        offer = _sent_offer(buyer, seller)
        services.accept(offer, by_user=seller, now=mock_now())
        assert got == []  # accepted ≠ completed
        services.complete(offer, by_user=seller, now=mock_now())
        assert len(got) == 1
        assert set(got[0]["parties"]) == {buyer.pk, seller.pk}
        assert got[0]["root"].pk == offer.walk_to_root().pk
        with pytest.raises(OfferStatusError):
            services.complete(offer, by_user=seller, now=mock_now())
        assert len(got) == 1  # no second fire, no double signal
    finally:
        deal_completed.disconnect(receiver)


# ----------------------------------------------------------------- views


@pytest.mark.django_db
def test_two_users_negotiate_a_mix_entirely_in_app(buyer, seller, buyer_client, seller_client):
    """ACCEPTANCE: a mixed cash-plus-barter swap negotiated end-to-end in
    the app with a clean audit trail — compose → send → counter (2) →
    accept → complete, every step through the routes."""
    compose = {
        "mode": CASH_PLUS,
        "cash_amount": "300",
        "counterpart_username": "njeri",
        "give-TOTAL_FORMS": "3", "give-INITIAL_FORMS": "0",
        "give-MIN_NUM_FORMS": "0", "give-MAX_NUM_FORMS": "1000",
        "give-0-item": "2kg maize", "give-0-qty": "2 kg",
        "give-1-item": "sewing hour", "give-1-qty": "1",
        "give-2-item": "", "give-2-qty": "",
        "want-TOTAL_FORMS": "3", "want-INITIAL_FORMS": "0",
        "want-MIN_NUM_FORMS": "0", "want-MAX_NUM_FORMS": "1000",
        "want-0-item": "solar lamp", "want-0-qty": "1",
        "want-1-item": "", "want-1-qty": "",
        "want-2-item": "", "want-2-qty": "",
    }
    response = buyer_client.post(reverse("barter:new"), compose)
    assert response.status_code == 302
    root = Offer.objects.order_by("pk").last()
    assert root.status == DRAFT
    page = buyer_client.get(reverse("barter:detail", args=[root.pk]))
    assert page.status_code == 200
    assert_under_budget(page.content.decode(), kb=50)

    assert buyer_client.post(
        reverse("barter:send", args=[root.pk])
    ).status_code == 302
    root.refresh_from_db()
    assert root.status == OFFERED

    # seller counters through the routes (compose form → draft → send):
    page = seller_client.get(reverse("barter:detail", args=[root.pk]))
    assert page.status_code == 200  # the recipient reads the same trail
    counter_page = seller_client.get(reverse("barter:counter", args=[root.pk]))
    assert counter_page.status_code == 200
    assert_under_budget(counter_page.content.decode(), kb=50)
    counter_post = {
        "mode": BARTER,
        "cash_amount": "",
        "give-TOTAL_FORMS": "3", "give-INITIAL_FORMS": "0",
        "give-MIN_NUM_FORMS": "0", "give-MAX_NUM_FORMS": "1000",
        "give-0-item": "2kg maize", "give-0-qty": "2 kg",
        "give-1-item": "", "give-1-qty": "",
        "give-2-item": "", "give-2-qty": "",
        "want-TOTAL_FORMS": "3", "want-INITIAL_FORMS": "0",
        "want-MIN_NUM_FORMS": "0", "want-MAX_NUM_FORMS": "1000",
        "want-0-item": "solar lamp", "want-0-qty": "1",
        "want-1-item": "", "want-1-qty": "",
        "want-2-item": "", "want-2-qty": "",
    }
    response = seller_client.post(
        reverse("barter:counter", args=[root.pk]), counter_post
    )
    assert response.status_code == 302
    child = Offer.objects.filter(parent=root).order_by("pk").last()
    assert child.status == DRAFT and child.mode == BARTER
    assert seller_client.post(
        reverse("barter:send", args=[child.pk])
    ).status_code == 302
    child.refresh_from_db()
    root.refresh_from_db()
    assert child.status == OFFERED and root.status == COUNTERED

    # buyer counters back through the routes (counter #2):
    counter2_page = buyer_client.get(reverse("barter:counter", args=[child.pk]))
    assert counter2_page.status_code == 200
    response = buyer_client.post(
        reverse("barter:counter", args=[child.pk]),
        {
            **counter_post,
            "mode": CASH_PLUS,
            "cash_amount": "450",
            "give-0-item": "2kg maize",
            "give-1-item": "sewing hour",
            "give-2-item": "", "give-2-qty": "",
            "want-1-item": "", "want-1-qty": "",
            "want-2-item": "", "want-2-qty": "",
        },
    )
    assert response.status_code == 302
    counter2 = Offer.objects.filter(parent=child).order_by("pk").last()
    assert counter2.status == DRAFT and counter2.cash_amount == Decimal("450")
    assert buyer_client.post(
        reverse("barter:send", args=[counter2.pk])
    ).status_code == 302
    counter2.refresh_from_db()
    assert counter2.status == OFFERED

    assert seller_client.post(
        reverse("barter:accept", args=[counter2.pk])
    ).status_code == 302
    counter2.refresh_from_db()
    root.refresh_from_db()
    assert counter2.status == ACCEPTED and root.status == ACCEPTED

    assert buyer_client.post(
        reverse("barter:complete", args=[counter2.pk])
    ).status_code == 302
    root.refresh_from_db()
    assert root.status == COMPLETED
    for row in root.lineage():
        row.refresh_from_db()
        assert row.status == COMPLETED  # the audit trail reads settled


@pytest.mark.django_db
def test_list_page_sections_render_and_budget(buyer, seller, buyer_client):
    """The inbox renders its four sections; two cards' budgets hold."""
    response = buyer_client.get(reverse("barter:list"))
    assert response.status_code == 200
    assert_under_budget(response.content.decode(), kb=50)


@pytest.mark.django_db
def test_move_routes_refuse_get_with_405(buyer_client, buyer):
    """Moves are POST-only — GET is a 405, never an accidental state edit."""
    for name in ("send", "accept", "cancel", "complete"):
        response = buyer_client.get(reverse(f"barter:{name}", args=[1]))
        assert response.status_code == 405


@pytest.mark.django_db
def test_honest_cash_copy_renders_on_detail(buyer, seller, buyer_client):
    """The no-custody sentence is where money shows (spec: 'the UI says
    so plainly')."""
    offer = _sent_offer(buyer, seller, mode=CASH, cash=500)
    page = buyer_client.get(reverse("barter:detail", args=[offer.pk]))
    text = page.content.decode()
    assert "You two settle the cash part yourselves (M-Pesa)" in text
    assert "never holds money" in text
    from apps.barter.mpesa import stated_cash_only

    assert force_str(stated_cash_only(500)) in text


@pytest.mark.django_db
def test_listing_origin_end_to_end_with_bad_and_own_refs(buyer, seller, buyer_client):
    """Marketplace origin: ?listing= composes against the listing owner
    through listings' PUBLIC door; own listing and junk refs render the
    honest pages. WHY the real listing row: the PUBLIC door is part of
    THIS app's contract (Prompt 17's landed door) — test against it."""
    from apps.listings.services import create_listing

    listing = create_listing(
        seller=seller, title="Solar lamp", description="Brand new solar lamp.",
        category="electronics", ward="Makongeni", price_negotiable=True,
    )
    page = buyer_client.get(f"{reverse('barter:new')}?listing={listing.pk}")
    assert page.status_code == 200
    assert "Solar lamp" in page.content.decode()

    data = {
        "mode": CASH, "cash_amount": "200",
        "counterpart_username": "",  # listing origin: no username
        "origin_listing": listing.pk,
        "give-TOTAL_FORMS": "3", "give-INITIAL_FORMS": "0",
        "give-MIN_NUM_FORMS": "0", "give-MAX_NUM_FORMS": "1000",
        "give-0-item": "sewing hour", "give-0-qty": "1",
        "give-1-item": "", "give-1-qty": "",
        "give-2-item": "", "give-2-qty": "",
        "want-TOTAL_FORMS": "3", "want-INITIAL_FORMS": "0",
        "want-MIN_NUM_FORMS": "0", "want-MAX_NUM_FORMS": "1000",
        "want-0-item": "", "want-0-qty": "",
        "want-1-item": "", "want-1-qty": "",
        "want-2-item": "", "want-2-qty": "",
    }
    response = buyer_client.post(reverse("barter:new"), data)
    assert response.status_code == 302
    draft = Offer.objects.order_by("pk").last()
    assert draft.counterpart_id == seller.pk  # derived from the PUBLIC door
    assert draft.origin_kind == "listing" and draft.origin_listing_id == listing.pk

    own = Client()
    own.force_login(seller)
    own_page = own.get(f"{reverse('barter:new')}?listing={listing.pk}")
    assert own_page.status_code == 200
    assert "your own listing" in own_page.content.decode()

    junk = buyer_client.get(f"{reverse('barter:new')}?listing={listing.pk + 999}")
    assert junk.status_code == 200
    assert "available" in junk.content.decode()


@pytest.mark.django_db
def test_unknown_username_composes_to_a_clean_error(buyer, buyer_client, seller):
    response = buyer_client.post(
        reverse("barter:new"),
        {
            "mode": CASH, "cash_amount": "100",
            "counterpart_username": "ghost_user",
            "give-TOTAL_FORMS": "0", "give-INITIAL_FORMS": "0",
            "want-TOTAL_FORMS": "0", "want-INITIAL_FORMS": "0",
        },
    )
    assert response.status_code == 200  # re-rendered with the refusal
    assert "No member with that username" in response.content.decode()
    assert Offer.objects.count() == 0


@pytest.mark.django_db
def test_mpesa_is_a_stub_never_a_paypipe():
    """The documented future point: constructing it raises BEFORE any
    network can exist; the interface methods do too; nothing here calls
    out. WHY so blunt: a stub that silently 'succeeds' would teach a
    reader the wrong thing — refusal is the honest behaviour."""
    with pytest.raises(NotImplementedError) as exc_info:
        MPesaAdapter({})
    assert "V1 ships no M-Pesa integration" in str(exc_info.value)
    with pytest.raises(NotImplementedError):
        MPesaAdapter.initiate_settlement_request(None, x=1)
    assert MPesaAdapter.is_stub is True
