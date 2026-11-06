"""Prompt 24 acceptance: listings age, expire, and renew WITHOUT moderator
load — and the one visibility lever a seller gets (the bump) is capped so
utility never becomes pay-to-shout.

Sections: the transition matrix (pure table + services), the 30-day expiry
sweep (injectable clock, boundary + renew anchor), renewal semantics
(identical content, fresh timestamp, one-tap), the bump cap, the
category-edit gate (bait-and-switch prevention), bump-aware list ordering,
and the visibility contract. Prompt 17's own suite (tests/test_listings.py)
is run alongside this file as the regression baseline.

Every clock-sensitive test injects mock_now() and ages rows explicitly —
nothing in here trusts the real wall clock, so the file is deterministic.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from apps.core.testing import assert_under_budget, mock_now
from apps.core.types import ContentStatus
from apps.listings import lifecycle as lc
from apps.listings import services
from apps.listings.models import Listing, ListingLifecycle
from apps.listings.services import (
    bump_listing,
    edit_listing_category,
    expire_stale_listings,
    mark_listing_sold,
    pause_listing,
    renew_listing,
    resume_listing,
)

PUBLISHED = ContentStatus.PUBLISHED.value


@pytest.fixture
def seller(fresh_user):
    return fresh_user


def day(n, **extra):
    from datetime import timedelta

    return timedelta(days=n, **extra)


def _create(seller, **overrides):
    """A real listing through the P17 create path (publish-on-create) —
    tests measure the flow production uses, never hand-made rows."""
    kwargs = dict(
        seller=seller,
        title="Jiko la makaa",
        description="Jiko lingine, linawaka vizuri.",
        category="furniture",
        ward="Kilimani",
        price=1500,
    )
    kwargs.update(overrides)
    return services.create_listing(**kwargs)


def _age(listing, **stamps):
    """Force row timestamps (auto_now_add ignores passed values, so aging
    goes through queryset.update), then re-read so in-memory callers see
    the aged truth — bump/expiry anchors read the ROW, not the test's copy."""
    Listing.objects.filter(pk=listing.pk).update(**stamps)
    listing.refresh_from_db()
    return listing


def _snapshot(listing):
    """The content a renew must NOT change (the no-re-typing contract)."""
    fields = ("title", "title_sw", "description", "description_sw",
              "category", "condition", "price", "price_negotiable", "ward")
    return {name: getattr(listing, name) for name in fields}


def _stranger():
    other = get_user_model().objects.create_user("mtu-ngine", password="pw-123456")
    client = Client()
    client.force_login(other)
    return other, client


# ------------------------------------------------------------------ transition matrix


def test_table_covers_every_state():
    """The table is contract: every lifecycle state has a row, and no
    invented state sneaks in. The Prompt-mandated windows live here too —
    the suite pins them so a silent retune cannot slip by."""
    assert set(lc.TRANSITIONS) == set(ListingLifecycle.values)
    assert lc.EXPIRY_DAYS == 30
    assert lc.BUMP_COOLDOWN_DAYS == 7


@pytest.mark.parametrize(
    "current,target",
    [
        ("ACTIVE", "PAUSED"),
        ("ACTIVE", "SOLD"),
        ("ACTIVE", "EXPIRED"),
        ("PAUSED", "ACTIVE"),
        ("PAUSED", "SOLD"),
        ("EXPIRED", "ACTIVE"),
    ],
)
def test_table_allows_the_legal_edges(current, target):
    assert lc.can_transition(current, target)
    lc.assert_transition(current, target)  # does not raise


@pytest.mark.parametrize(
    "current,target",
    [
        ("ACTIVE", "ACTIVE"),
        ("PAUSED", "EXPIRED"),
        ("SOLD", "ACTIVE"),
        ("SOLD", "PAUSED"),
        ("SOLD", "SOLD"),
        ("SOLD", "EXPIRED"),
        ("EXPIRED", "PAUSED"),
        ("EXPIRED", "EXPIRED"),
    ],
)
def test_table_refuses_the_illegal_edges(current, target):
    """SOLD is terminal (a re-list is a NEW listing); EXPIRED returns ONLY
    via the one-tap renew; same-state is a refusal (no-op moves belong to
    the seller, not the machine)."""
    assert not lc.can_transition(current, target)
    with pytest.raises(lc.LifecycleError):
        lc.assert_transition(current, target)


# ------------------------------------------------------------------ service matrix


@pytest.mark.django_db
def test_pause_then_resume_round_trip_keeps_content(seller):
    listing = _create(seller)
    paused = pause_listing(listing=listing, by_user=seller, now=mock_now())
    assert paused.lifecycle == ListingLifecycle.PAUSED.value
    assert paused.paused_at == mock_now()
    assert paused.status == PUBLISHED            # moderation axis untouched
    resumed = resume_listing(listing=paused, by_user=seller, now=mock_now())
    assert resumed.lifecycle == ListingLifecycle.ACTIVE.value
    assert resumed.paused_at is None             # a CURRENT-state stamp, resumed
    for name, value in _snapshot(listing).items():
        assert getattr(resumed, name) == value   # content byte-for-byte


@pytest.mark.django_db
def test_mark_sold_from_active_or_paused_is_terminal(seller):
    from_active = _create(seller)
    mark_listing_sold(listing=from_active, by_user=seller, now=mock_now())
    assert from_active.lifecycle == ListingLifecycle.SOLD.value
    assert from_active.sold_at == mock_now()
    # Every further seller move is refused: SOLD is the end of THIS row
    # (a re-list of the item is a NEW listing — lifecycle.py docstring).
    for _verb, action in SELLER_ACTIONS:
        if action is mark_listing_sold:
            continue
        with pytest.raises(lc.LifecycleError):
            action(listing=from_active, by_user=seller, now=mock_now())
    # PAUSED can still be sold (seller closes out mid-hold).
    held = pause_listing(listing=_create(seller), by_user=seller, now=mock_now())
    mark_listing_sold(listing=held, by_user=seller, now=mock_now())
    assert held.lifecycle == ListingLifecycle.SOLD.value
    assert held.sold_at == mock_now()


@pytest.mark.django_db
def test_renew_semantics_identical_content_fresh_timestamp(seller):
    """THE renewal contract: same row, IDENTICAL content, fresh renewed_at —
    one tap, no re-typing, no moderator, no second row. Expired honestly by
    the sweep first (the way rows die in production)."""
    listing = _create(seller)
    original = _snapshot(listing)
    _age(listing, created_at=mock_now() - day(45))
    assert expire_stale_listings(now=mock_now()) == 1
    listing.refresh_from_db()
    assert listing.lifecycle == ListingLifecycle.EXPIRED.value

    renewed = renew_listing(listing=listing, by_user=seller, now=mock_now())
    assert renewed is listing                    # same row — never a clone
    assert renewed.lifecycle == ListingLifecycle.ACTIVE.value
    assert renewed.renewed_at == mock_now()      # THE fresh timestamp
    assert renewed.status == PUBLISHED           # moderation axis never touched
    for name, value in original.items():         # identical content
        assert getattr(renewed, name) == value
    # and the new anchor re-arms exactly one window: renewed 0 days ago →
    # nothing stale today, exactly 30 days later it expires again.
    assert expire_stale_listings(now=mock_now()) == 0
    assert expire_stale_listings(now=mock_now() + day(30)) == 1


@pytest.mark.django_db
def test_renew_refused_on_active_row(seller):
    listing = _create(seller)
    with pytest.raises(lc.LifecycleError):
        renew_listing(listing=listing, by_user=seller, now=mock_now())
    assert listing.lifecycle == ListingLifecycle.ACTIVE.value


@pytest.mark.django_db
def test_pause_refused_on_sold_row(seller):
    listing = _create(seller)
    mark_listing_sold(listing=listing, by_user=seller, now=mock_now())
    with pytest.raises(lc.LifecycleError):
        pause_listing(listing=listing, by_user=seller, now=mock_now())


@pytest.mark.django_db
def test_resume_on_expired_refused_without_renew(seller):
    """EXPIRED → ACTIVE is strictly the one-tap renew (fresh timestamp):
    a plain resume refuses, so "back on the market" always means the seller
    re-stamped the row."""
    listing = _create(seller)
    _age(listing, created_at=mock_now() - day(31))
    expire_stale_listings(now=mock_now())
    listing.refresh_from_db()
    with pytest.raises(lc.LifecycleError):
        resume_listing(listing=listing, by_user=seller, now=mock_now())


@pytest.mark.django_db
def test_resume_refused_on_active_row(seller):
    listing = _create(seller)
    with pytest.raises(lc.LifecycleError):
        resume_listing(listing=listing, by_user=seller, now=mock_now())


SELLER_ACTIONS = (
    ("pause", pause_listing),
    ("resume", resume_listing),
    ("mark sold", mark_listing_sold),
    ("renew", renew_listing),
    ("bump", bump_listing),
)


@pytest.mark.django_db
@pytest.mark.parametrize("verb,action", SELLER_ACTIONS)
def test_non_party_transitions_refused(seller, verb, action):
    """Nobody moves someone else's listing (staff keep the P17 allowance
    for moderation). The row is unchanged after every refusal."""
    listing = _create(seller)
    stranger, _client = _stranger()
    before = _snapshot(listing)
    with pytest.raises(PermissionDenied):
        action(listing=listing, by_user=stranger, now=mock_now())
    listing.refresh_from_db()
    assert _snapshot(listing) == before


# ------------------------------------------------------------------ expiry sweep


@pytest.mark.django_db
def test_sweep_boundary_exactly_30_days(seller):
    """Boundary contract (mirroring barter's sweep): at EXACTLY EXPIRY_DAYS
    elapsed the row moves; a same-instant fresh row does not."""
    boundary = _age(_create(seller), created_at=mock_now() - day(30))
    fresh = _age(_create(seller), created_at=mock_now())
    assert expire_stale_listings(now=mock_now()) == 1
    boundary.refresh_from_db()
    fresh.refresh_from_db()
    assert boundary.lifecycle == ListingLifecycle.EXPIRED.value
    assert fresh.lifecycle == ListingLifecycle.ACTIVE.value


@pytest.mark.django_db
def test_sweep_respects_the_renew_anchor(seller):
    """Age anchor = renewed_at when set, else created_at: a renewed row is
    30 days old measured from its LAST renewal, not its birth."""
    renewed_long_ago = _age(
        _create(seller), created_at=mock_now() - day(100),
        renewed_at=mock_now() - day(30),
    )
    renewed_recently = _age(
        _create(seller), created_at=mock_now() - day(100),
        renewed_at=mock_now() - day(29, seconds=1),
    )
    assert expire_stale_listings(now=mock_now()) == 1
    renewed_long_ago.refresh_from_db()
    renewed_recently.refresh_from_db()
    assert renewed_long_ago.lifecycle == ListingLifecycle.EXPIRED.value
    assert renewed_recently.lifecycle == ListingLifecycle.ACTIVE.value


@pytest.mark.django_db
def test_sweep_never_sweeps_off_market_or_pending_rows(seller):
    """Only PUBLISHED+ACTIVE rows are candidates: draft stock, a seller's
    paused hold, moderation-pending (e.g. category-gated) rows and removed
    rows keep their state — expiry is a MARKET job, not a janitor. Rows the
    text detector flagged but still PUBLISHED+ACTIVE are NOT exempt: the
    flag is a moderation annotation, orthogonal to market age (mirror of
    the two-axis rule)."""
    aged = day(100)
    paused = pause_listing(
        listing=_age(_create(seller), created_at=mock_now() - aged),
        by_user=seller, now=mock_now(),
    )
    draft = _create(
        seller, title="Draft stock", status=ContentStatus.DRAFT.value
    )
    flagged = _create(seller)
    flagged.description = "piga simu 0712 345 678"
    services.flag_listing_phone_patterns(flagged)  # flagged, still PUBLISHED
    _age(flagged, created_at=mock_now() - aged)
    gated = _age(
        services.create_listing(
            seller=seller, title="Pending jiko", description=".",
            category="tools", ward="Umoja", price=1
        ),
        created_at=mock_now() - aged,
    )
    services.edit_listing_category(
        listing=gated, editor=seller, category="handmade"
    )  # the P24 gate: → UNDER_REVIEW, off the list but still trading

    # sweep(1) moves ONLY the flagged-but-live row...
    assert expire_stale_listings(now=mock_now()) == 1
    paused.refresh_from_db()
    draft.refresh_from_db()
    gated.refresh_from_db()
    flagged.refresh_from_db()
    assert paused.lifecycle == ListingLifecycle.PAUSED.value
    assert draft.status == ContentStatus.DRAFT.value
    assert (gated.status == ContentStatus.UNDER_REVIEW.value
            and gated.lifecycle == ListingLifecycle.ACTIVE.value)
    assert flagged.lifecycle == ListingLifecycle.EXPIRED.value  # market-age wins
    # ...and a second sweep has nothing left to do.
    assert expire_stale_listings(now=mock_now()) == 0


@pytest.mark.django_db
def test_sweep_is_idempotent_and_returns_count(seller):
    a = _age(_create(seller), created_at=mock_now() - day(30))
    b = _age(_create(seller), created_at=mock_now() - day(35))
    assert expire_stale_listings(now=mock_now()) == 2
    assert expire_stale_listings(now=mock_now()) == 0  # nothing stale twice
    a.refresh_from_db()
    b.refresh_from_db()
    assert {row.lifecycle for row in (a, b)} == {ListingLifecycle.EXPIRED.value}


# ------------------------------------------------------------------ bump cap


@pytest.mark.django_db
def test_bump_first_at_exactly_creation_plus_seven_days(seller):
    """The cap anchor is coalesce(bumped_at, created_at) — creation counts
    as the initial position refresh, so brand-new rows (already at the top
    of the feed) cannot bump inside the first week. At EXACTLY 7 days the
    first bump goes through (the mirror of the expiry boundary)."""
    listing = _age(_create(seller), created_at=mock_now())
    bumped = bump_listing(
        listing=listing, by_user=seller, now=mock_now() + day(7)
    )
    assert bumped.bumped_at == mock_now() + day(7)


@pytest.mark.django_db
def test_bump_second_inside_window_refused_then_after_ok(seller):
    listing = _age(_create(seller), created_at=mock_now() - day(8))
    first = bump_listing(listing=listing, by_user=seller, now=mock_now())
    assert first.bumped_at == mock_now()
    with pytest.raises(lc.LifecycleError):       # 6d23h59m after the bump
        bump_listing(
            listing=listing, by_user=seller,
            now=mock_now() + day(6, hours=23, minutes=59),
        )
    listing.refresh_from_db()
    assert listing.bumped_at == mock_now()       # the refusal wrote NOTHING
    again = bump_listing(
        listing=listing, by_user=seller, now=mock_now() + day(7)
    )
    assert again.bumped_at == mock_now() + day(7)


@pytest.mark.django_db
def test_bump_refused_on_paused_row(seller):
    listing = pause_listing(
        listing=_age(_create(seller), created_at=mock_now() - day(10)),
        by_user=seller, now=mock_now(),
    )
    with pytest.raises(lc.LifecycleError):
        bump_listing(listing=listing, by_user=seller, now=mock_now())


@pytest.mark.django_db
def test_bump_route_one_tap_then_capped(client_auth, fresh_user):
    """Route surface: first POST bumps, second POST lands back on detail
    with the plain-language flash and bumps_at untouched (no 500, no
    silent success)."""
    listing = _age(_create(fresh_user), created_at=mock_now() - day(8))
    assert client_auth.post(f"/market/{listing.pk}/bump/").status_code == 302
    listing.refresh_from_db()
    stamp = listing.bumped_at
    assert stamp is not None
    assert client_auth.post(f"/market/{listing.pk}/bump/").status_code == 302
    listing.refresh_from_db()
    assert listing.bumped_at == stamp            # the capped attempt did nothing


# ------------------------------------------------------------------ category gate


@pytest.mark.django_db
def test_gate_category_change_on_live_row_flips_to_review(seller):
    """THE bait-and-switch gate: a live (PUBLISHED+ACTIVE) row whose seller
    changes the category flips ContentStatus.UNDER_REVIEW + review_flagged —
    while lifecycle stays ACTIVE (the seller still wants to trade; the
    MODERATION axis is what hides the row — two independent axes)."""
    listing = _create(seller)                    # furniture
    gated = edit_listing_category(
        listing=listing, editor=seller, category="electronics"
    )
    assert gated.category == "electronics"       # the intent IS recorded
    assert gated.status == ContentStatus.UNDER_REVIEW.value
    assert gated.category_gate is True   # pending state on ITS OWN column,
    assert gated.review_flagged is False  # the phone flag is untouched
    assert gated.lifecycle == ListingLifecycle.ACTIVE.value
    assert listing.pk not in [row.pk for row in services.visible_listings()]


@pytest.mark.django_db
def test_price_edits_stay_active_and_published(seller):
    """Edit rules, the free side: price (and description) edits while
    ACTIVE touch neither the status nor the lifecycle — negotiation is not
    deception."""
    listing = _create(seller)
    edited = services.edit_listing(
        listing=listing, editor=seller, price=1800, description="Bei mpya kidogo."
    )
    assert edited.price == 1800
    assert edited.status == PUBLISHED
    assert edited.lifecycle == ListingLifecycle.ACTIVE.value
    assert edited.category == "furniture"        # the gate saw no category


@pytest.mark.django_db
def test_gate_only_gates_live_market_rows(seller):
    """Off-market rows are not buyer-facing deception: their category saves
    plainly, no moderation flip.
    - DRAFT: saves, stays DRAFT.
    - EXPIRED: saves, stays PUBLISHED (status) — the row is off the market;
      a moderator re-approval of a hidden row would be dead ceremony."""
    draft = services.create_listing(
        seller=seller, title="Draft stock", description=".",
        category="tools", ward="Umoja", price=1,
        status=ContentStatus.DRAFT.value,
    )
    saved = edit_listing_category(
        listing=draft, editor=seller, category="furniture"
    )
    assert saved.category == "furniture"
    assert saved.status == ContentStatus.DRAFT.value
    assert saved.review_flagged is False

    stale = _age(_create(seller), created_at=mock_now() - day(30))
    expire_stale_listings(now=mock_now())
    stale.refresh_from_db()
    assert stale.lifecycle == ListingLifecycle.EXPIRED.value
    saved2 = edit_listing_category(
        listing=stale, editor=seller, category="electronics"
    )
    assert saved2.category == "electronics"
    assert saved2.status == PUBLISHED
    assert saved2.lifecycle == ListingLifecycle.EXPIRED.value
    assert saved2.review_flagged is False


@pytest.mark.django_db
def test_gate_same_category_is_a_no_op(seller):
    listing = _create(seller)
    again = edit_listing_category(
        listing=listing, editor=seller, category="furniture"
    )
    assert again.status == PUBLISHED and again.review_flagged is False


@pytest.mark.django_db
def test_gate_refuses_unknown_category(seller):
    listing = _create(seller)
    with pytest.raises(ValidationError):
        edit_listing_category(listing=listing, editor=seller, category="spaceship")
    listing.refresh_from_db()
    assert listing.category == "furniture"


@pytest.mark.django_db
def test_non_party_category_gate_refused(seller):
    listing = _create(seller)
    stranger, _client = _stranger()
    with pytest.raises(PermissionDenied):
        edit_listing_category(listing=listing, editor=stranger, category="tools")
    listing.refresh_from_db()
    assert listing.category == "furniture"


@pytest.mark.django_db
def test_gate_via_route_hides_from_list_but_reachable_for_seller(
    client_auth, fresh_user
):
    """The member surface: the panel's mini-form posts /market/<pk>/category/
    → the row goes UNDER_REVIEW: hidden from the market for EVERYONE, still
    open on detail for the seller with the status badge — the re-approval
    return path is the moderators' queue (documented, not built here)."""
    listing = _create(fresh_user)
    assert client_auth.post(
        f"/market/{listing.pk}/category/", {"category": "electronics"}
    ).status_code == 302
    listing.refresh_from_db()
    assert listing.status == ContentStatus.UNDER_REVIEW.value
    assert listing.category_gate is True   # parent consolidation: own column
    market = Client().get("/market/")
    assert listing.pk not in [row.pk for row in market.context["listings"]]
    own = client_auth.get(f"/market/{listing.pk}/")
    assert own.status_code == 200 and b"Under review" in own.content


# ------------------------------------------------------------------ ordering


@pytest.mark.django_db
def test_market_list_bump_aware_ordering_is_deterministic(seller):
    """Bump = the ONLY route to the top, throttled by the cap; the order
    key is coalesce(bumped_at, created_at) descending, created_at breaks
    ties. Deterministic: same data, same order, every render."""
    old_row = _age(_create(seller), created_at=mock_now() - day(40))
    mid_row = _age(_create(seller), created_at=mock_now() - day(20))
    new_row = _age(_create(seller), created_at=mock_now())
    assert [row.pk for row in services.visible_listings()] == [
        new_row.pk, mid_row.pk, old_row.pk,
    ]
    # The seller bumps the OLDEST row (cap: 40 > 7 days since its anchor).
    bump_listing(listing=old_row, by_user=seller, now=mock_now() + day(1))
    assert [row.pk for row in services.visible_listings()] == [
        old_row.pk, new_row.pk, mid_row.pk,
    ]


@pytest.mark.django_db
def test_bumped_ties_break_deterministically_by_creation(seller):
    """Two rows bumped at the same injected instant tie on the order key;
    the created_at tiebreak keeps the feed reproducible."""
    first = _age(_create(seller), created_at=mock_now() - day(10))
    second = _age(_create(seller), created_at=mock_now() - day(9))
    for row in (first, second):
        bump_listing(listing=row, by_user=seller, now=mock_now())
    assert [row.pk for row in services.visible_listings()] == [
        second.pk, first.pk,
    ]


# ------------------------------------------------------------------ visibility + page weight


@pytest.mark.django_db
def test_market_list_shows_only_lifecycle_active_rows(seller):
    """PUBLISHED is necessary, no longer sufficient: PAUSED/SOLD/EXPIRED
    leave the public market for EVERY viewer but stay on their detail pages
    (badge renders plainly for buyers too)."""
    live = _create(seller)
    paused = pause_listing(
        listing=_create(seller, title="Held jiko"), by_user=seller, now=mock_now()
    )
    sold = mark_listing_sold(
        listing=_create(seller, title="Sold jiko"), by_user=seller, now=mock_now()
    )
    expired = _age(_create(seller, title="Stale jiko"),
                   created_at=mock_now() - day(31))
    expire_stale_listings(now=mock_now())
    public_pks = [row.pk for row in services.visible_listings()]
    assert live.pk in public_pks and expired.pk not in public_pks
    for gone in (paused, sold, expired):
        assert gone.pk not in public_pks
        assert Client().get(f"/market/{gone.pk}/").status_code == 200
    assert b"Expired" in Client().get(f"/market/{expired.pk}/").content


@pytest.mark.django_db
def test_detail_panel_shows_the_legal_moves_only(client_auth, fresh_user):
    """Each lifecycle state renders EXACTLY the moves the table makes legal
    — nothing else, and renew exists only for EXPIRED rows."""
    listing = _create(fresh_user)
    own = client_auth.get(f"/market/{listing.pk}/").content
    # ACTIVE: pause/sold/bump + the gated category door, never renew.
    assert f"/market/{listing.pk}/pause/".encode() in own
    assert f"/market/{listing.pk}/sold/".encode() in own
    assert f"/market/{listing.pk}/bump/".encode() in own
    assert f"/market/{listing.pk}/category/".encode() in own
    assert f"/market/{listing.pk}/renew/".encode() not in own

    pause_listing(listing=listing, by_user=fresh_user, now=mock_now())
    own = client_auth.get(f"/market/{listing.pk}/").content
    assert b"Paused" in own                      # the plain badge
    assert f"/market/{listing.pk}/resume/".encode() in own
    assert f"/market/{listing.pk}/pause/".encode() not in own

    resume_listing(listing=listing, by_user=fresh_user, now=mock_now())
    _age(listing, created_at=mock_now() - day(30))
    expire_stale_listings(now=mock_now())
    listing.refresh_from_db()
    own = client_auth.get(f"/market/{listing.pk}/").content
    assert b"Expired" in own and f"/market/{listing.pk}/renew/".encode() in own

    # THE one-tap renew (the only legal way out of EXPIRED), then close out.
    renew_listing(listing=listing, by_user=fresh_user, now=mock_now())
    listing.refresh_from_db()
    mark_listing_sold(listing=listing, by_user=fresh_user, now=mock_now())
    listing.refresh_from_db()
    own = client_auth.get(f"/market/{listing.pk}/").content
    assert b"Sold" in own
    assert f"/market/{listing.pk}/renew/".encode() not in own  # terminal


@pytest.mark.django_db
def test_seller_and_market_pages_stay_under_budget(client_auth, fresh_user):
    """The hard budget rule holds with the lifecycle panel added."""
    listing = _create(fresh_user)
    detail = client_auth.get(f"/market/{listing.pk}/").content.decode()
    market = client_auth.get("/market/").content.decode()
    assert_under_budget(detail, kb=50)
    assert_under_budget(market, kb=50)

@pytest.mark.django_db
def test_moderator_can_approve_a_recategorization(seller, monkeypatch):
    """The gate's other half (parent consolidation): a moderator returns the
    gated row to PUBLISHED in the new category, clears category_gate, and an
    immutable audit row records it. Sellers cannot self-approve."""
    from apps.moderation.models import AuditAction, ModeratorAudit
    from apps.listings.services import approve_recategorize

    listing = _create(seller)
    gated = edit_listing_category(listing=listing, editor=seller, category="electronics")
    staff = get_user_model().objects.create_user(
        "mod_probe", password="pw-12345", is_staff=True
    )
    with pytest.raises(PermissionDenied):
        approve_recategorize(listing=gated, by_user=seller)   # no self-approval
    approved = approve_recategorize(listing=gated, by_user=staff)
    assert approved.status == ContentStatus.PUBLISHED.value
    assert approved.category_gate is False
    assert approved.category == "electronics"
    assert ModeratorAudit.objects.filter(
        target_id=approved.pk, action=AuditAction.RECAT_APPROVED
    ).exists()
