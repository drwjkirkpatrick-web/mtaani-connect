"""Prompt 22 acceptance: two-phase publish (pair same-tick + 14-day
unilateral on an injected clock), star/text/1-star-note validation, the
mutual one-review-per-deal constraint, rollup math with tie-break
determinism, and honest deal-ref handling for unlanded origin apps.

SEMANTICS under test: a review is created HELD (DRAFT) — no route, no
service call, no test shortcut publishes a row except the sweep
(publish_due_reviews). Pair rows for the 'listings' origin are exercised at
the data level (models) because the listings door allows buyer→seller only;
the sweep's pair rule is origin-generic by contract (barter plugs through
its door when Prompt 21 lands — see the stub-door tests).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from apps.core.testing import assert_under_budget, client_auth, fresh_user, mock_now
from apps.listings import services as listings_services
from apps.reviews import models
from apps.reviews import services as svc
from apps.reviews.models import DealReview
from apps.trust import services as trust_services

NOW = mock_now()  # 2026-01-15 09:30 UTC (the injected evaluation clock)


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="bena", email="bena@example.com", password="***"
    )


@pytest.fixture
def third_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="chai", email="chai@example.com", password="***"
    )


@pytest.fixture
def seller_listing(other_user):
    """A published listing by `other_user` — the deal being reviewed."""
    return listings_services.create_listing(
        seller=other_user,
        title="Fresh sukuma wiki — 2 kg",
        description="Cut this morning, Kivuli road farm.",
        category="farm produce",
        ward="Kilimani",
        price=Decimal("250"),
    )


def _shift_created(review, when) -> None:
    """Stamp a DRAFT row's creation clock by hand. created_at is auto_now_add
    (an INSERT-time stamp — it cannot take an injected value), while the
    sweep's boundary math (created_at + 14d) must be deterministic to the
    second: the injected clock governs EVALUATION, so the row's 'written at'
    is pinned explicitly here."""
    review.created_at = when
    review.save(update_fields=["created_at"])


def _held_buyer_review(fresh_user, listing, stars=5, **kwargs):
    """Buyer→seller review through the PUBLIC service — lands DRAFT."""
    review = svc.create_deal_review(
        reviewer=fresh_user,
        deal_app="listings",
        deal_id=listing.pk,
        stars=stars,
        **kwargs,
    )
    _shift_created(review, NOW)
    return review


# ------------------------------------------------- two-phase publication


@pytest.mark.django_db
def test_both_sides_submit_publishes_in_same_tick(fresh_user, other_user, monkeypatch):
    """The mutual pair: A reviews B and B reviews A → the sweep publishes
    both rows with one shared published_at tick (simultaneous, per spec)."""
    class _BarterDoor:
        @staticmethod
        def review_for(deal_id, reviewer_id):
            # The barter contract: parties traded once; each reviews the other.
            return {fresh_user.pk: other_user.pk, other_user.pk: fresh_user.pk}.get(
                reviewer_id
            )

        @staticmethod
        def deal_label(deal_id):
            return "Barter offer — maize swap for solar lamp"

    monkeypatch.setattr(
        svc, "_origin_services",
        lambda deal_app: _BarterDoor if deal_app == "barter" else None,
    )
    first = svc.create_deal_review(
        reviewer=fresh_user, deal_app="barter", deal_id=12, stars=5, text="Poa."
    )
    second = svc.create_deal_review(
        reviewer=other_user, deal_app="barter", deal_id=12, stars=4
    )
    assert first.status == "DRAFT" and second.status == "DRAFT"  # held on submit

    sweep_at = NOW + timedelta(days=2)
    published = svc.publish_due_reviews(now=sweep_at)
    assert {row.pk for row in published} == {first.pk, second.pk}
    first.refresh_from_db(fields=["status", "published_at"])
    second.refresh_from_db(fields=["status", "published_at"])
    assert first.status == "PUBLISHED" == second.status
    assert first.published_at == second.published_at == sweep_at  # same tick


@pytest.mark.django_db
def test_lone_draft_is_held_before_day_14(fresh_user, other_user, seller_listing):
    review = _held_buyer_review(fresh_user, seller_listing, stars=5)
    assert svc.publish_due_reviews(now=NOW + timedelta(days=13)) == []
    review.refresh_from_db()
    assert review.status == "DRAFT"  # still retaliatory-window-open
    # Held rows never leak into reputation or the received page either.
    assert svc.reputation_for(other_user, now=NOW + timedelta(days=13))["score"] == 0.0


@pytest.mark.django_db
def test_unilateral_publish_exactly_at_day_14_boundary(
    fresh_user, other_user, seller_listing
):
    review = _held_buyer_review(fresh_user, seller_listing, stars=2)
    release = NOW + timedelta(days=14)  # boundary day INCLUSIVE (spec: 14 days)
    published = svc.publish_due_reviews(now=release)
    assert [row.pk for row in published] == [review.pk]
    review.refresh_from_db(fields=["status", "published_at"])
    assert review.status == "PUBLISHED"
    assert review.published_at == release  # the sweep's tick, exactly


@pytest.mark.django_db
def test_sweep_is_idempotent(fresh_user, other_user, seller_listing):
    review = _held_buyer_review(fresh_user, seller_listing, stars=3, text="Fine.")
    first = svc.publish_due_reviews(now=NOW + timedelta(days=15))
    second = svc.publish_due_reviews(now=NOW + timedelta(days=15, seconds=1))
    assert [row.pk for row in first] == [review.pk] and second == []
    review.refresh_from_db(fields=["published_at", "status"])
    assert review.published_at == NOW + timedelta(days=15)  # never re-stamped
    assert second == []


# ----------------------------------------------------- mutual constraints


@pytest.mark.django_db
def test_double_review_rejected(fresh_user, other_user, seller_listing):
    _held_buyer_review(fresh_user, seller_listing, stars=4)
    with pytest.raises(ValidationError) as excinfo:
        _held_buyer_review(fresh_user, seller_listing, stars=1, dispute_note="x" * 20)
    assert svc.MSG_ALREADY_REVIEWED in [str(m) for m in excinfo.value.messages]


@pytest.mark.django_db
def test_uniqueness_backed_by_the_db(fresh_user, other_user, seller_listing):
    _held_buyer_review(fresh_user, seller_listing, stars=5)
    row = DealReview(
        reviewer=fresh_user,
        reviewed_id=other_user.pk,
        deal_app="listings",
        deal_id=seller_listing.pk,
        stars=4,
    )
    with pytest.raises(IntegrityError):  # constraint, not the service check
        row.save()


@pytest.mark.django_db
def test_self_review_rejected(fresh_user, other_user, seller_listing):
    with pytest.raises(ValidationError) as excinfo:
        svc.create_deal_review(
            reviewer=other_user,  # the seller, on their own listing
            deal_app="listings",
            deal_id=seller_listing.pk,
            stars=5,
            text="Best deal ever, obviously.",
        )
    assert svc.MSG_SELF_REVIEW in [str(m) for m in excinfo.value.messages]
    assert DealReview.objects.count() == 0


@pytest.mark.django_db
def test_stranger_and_unknown_deal_refs_rejected(fresh_user, seller_listing):
    with pytest.raises(ValidationError):
        svc.create_deal_review(
            reviewer=fresh_user, deal_app="listings",
            deal_id=424242, stars=3,  # no such deal
        )
    with pytest.raises(ValidationError):
        svc.create_deal_review(
            reviewer=fresh_user, deal_app="unknown-app", deal_id=1, stars=3,
        )  # outside the allow-list — no such deal origin exists
    assert DealReview.objects.count() == 0


@pytest.mark.django_db
def test_unlanded_origins_fail_honestly(fresh_user):
    """Barter/services doors are NOT landed here — reviews must refuse with
    the honest unreadable message, never guess a deal or parties."""
    for origin in ("barter", "services"):
        with pytest.raises(ValidationError) as excinfo:
            svc.create_deal_review(
                reviewer=fresh_user, deal_app=origin, deal_id=1, stars=3
            )
        assert svc.MSG_DEAL_NOT_READABLE in [str(m) for m in excinfo.value.messages]
    assert DealReview.objects.count() == 0


@pytest.mark.django_db
def test_broken_origin_door_is_honest_absence(fresh_user, other_user, monkeypatch):
    """A door that raises (concurrently-landing sibling, transient state)
    must read as 'deal unreadable', not as a crash — the sweep/label paths
    degrade to the fallback too."""

    class _BrokenDoor:
        @staticmethod
        def review_for(deal_id, reviewer_id):
            raise RuntimeError("sibling mid-write")

    monkeypatch.setattr(
        svc, "_origin_services", lambda deal_app: _BrokenDoor if deal_app == "barter" else None,
    )
    with pytest.raises(ValidationError):
        svc.create_deal_review(
            reviewer=fresh_user, deal_app="barter", deal_id=77, stars=3
        )
    assert svc.deal_label("barter", 77) == "Deal #77"


@pytest.mark.django_db
def test_deal_labels_are_honest(fresh_user, other_user, seller_listing, monkeypatch):
    _held_buyer_review(fresh_user, seller_listing, stars=4)
    assert svc.deal_label("listings", seller_listing.pk) == seller_listing.title
    assert svc.deal_label("listings", 424242) == "Deal #424242"  # fallback


@pytest.mark.django_db
def test_pair_rule_is_origin_generic_at_data_level(
    fresh_user, other_user, seller_listing
):
    """The sweep pairs ANY two held rows forming A↔B on one deal — no
    origin special-casing. The seller→buyer row is written directly because
    the listings door allows buyer→seller only (no buyer identity exists on
    listings yet); barter pairs arrive through the door (tested above)."""
    buyer_row = _held_buyer_review(fresh_user, seller_listing, stars=1,
                                   dispute_note="They sold me dry maize, weight short and no reply.")
    seller_row = DealReview(
        reviewer=other_user,
        reviewed_id=fresh_user.pk,
        deal_app="listings",
        deal_id=seller_listing.pk,
        stars=4,
        text="Fair buyer, paid as agreed.",
    )
    seller_row.full_clean()
    seller_row.save()
    _shift_created(seller_row, NOW)
    published = svc.publish_due_reviews(now=NOW + timedelta(days=1))
    assert {row.pk for row in published} == {buyer_row.pk, seller_row.pk}
    seller_row.refresh_from_db()
    buyer_row.refresh_from_db()
    assert seller_row.published_at == buyer_row.published_at == NOW + timedelta(days=1)


# --------------------------------------------------- stars/text validation


@pytest.mark.django_db
@pytest.mark.parametrize("bad_stars", [0, 6, -1])
def test_star_bounds_rejected(fresh_user, other_user, seller_listing, bad_stars):
    with pytest.raises(ValidationError) as excinfo:
        svc.create_deal_review(
            reviewer=fresh_user, deal_app="listings",
            deal_id=seller_listing.pk, stars=bad_stars,
        )
    assert svc.MSG_STAR_BOUNDS in [str(m) for m in excinfo.value.messages]


@pytest.mark.django_db
def test_text_cap_enforced(fresh_user, other_user, seller_listing):
    with pytest.raises(ValidationError):
        _held_buyer_review(
            fresh_user, seller_listing, stars=5, text="x" * 501,
        )
    ok = _held_buyer_review(
        fresh_user, seller_listing, stars=5, text="x" * 500,
    )
    assert ok.text == "x" * 500


# -------------------------------------------------- 1-star dispute gating


@pytest.mark.django_db
def test_one_star_requires_a_dispute_note(fresh_user, other_user, seller_listing):
    with pytest.raises(ValidationError) as bare:
        _held_buyer_review(fresh_user, seller_listing, stars=1)
    assert models.MSG_DISPUTE_NOTE_REQUIRED in [str(m) for m in bare.value.messages]
    with pytest.raises(ValidationError):
        _held_buyer_review(
            fresh_user, seller_listing, stars=1,
            dispute_note="too short",  # < 20 chars
        )
    ok = _held_buyer_review(
        fresh_user, seller_listing, stars=1,
        dispute_note="Item was not what the description said.",  # ≥20 chars
    )
    assert ok.status == "DRAFT"


@pytest.mark.django_db
def test_non_one_star_needs_no_dispute_note(fresh_user, other_user, seller_listing):
    # A 2-star review carries NO dispute note (the gate is 1-star only)…
    _held_buyer_review(fresh_user, seller_listing, stars=2)
    # …on the SECOND deal too (one review per reviewer per deal holds here:
    # the same reviewer across a different deal id is a different row).
    second = listings_services.create_listing(
        seller=other_user, title="Second bunch", description="Same farm.",
        category="farm produce", ward="Kilimani", price=Decimal("60"),
    )
    _held_buyer_review(fresh_user, second, stars=3)
    assert DealReview.objects.filter(status="DRAFT").count() == 2


def test_dispute_gate_contract_for_prompt_23(monkeypatch):
    """When Prompt 23 lands, the 1-star note binds to OPEN disputes on the
    deal. Prove the gate narrows (not disappears) without that app existing:
    stub its public door via sys.modules and assert both branches."""
    import sys
    from types import ModuleType

    def _stub(returns: bool) -> ModuleType:
        module = ModuleType("apps.disputes.services")
        module.dispute_open_for_deal = lambda deal_app, deal_id: returns
        return module

    try:
        # Today: no disputes app → the note is required for EVERY 1-star.
        assert models._dispute_note_required_for(1, "listings", 1) is True
        # After 23 lands with an open dispute on this deal → still required…
        monkeypatch.setitem(sys.modules, "apps.disputes.services", _stub(True))
        monkeypatch.setitem(sys.modules, "apps.disputes", ModuleType("apps.disputes"))
        assert models._dispute_note_required_for(1, "listings", 1) is True
        # …and NOT required when no dispute is open (the spec's tightening).
        monkeypatch.setitem(sys.modules, "apps.disputes.services", _stub(False))
        assert models._dispute_note_required_for(1, "listings", 1) is False
    finally:
        # sys.modules stubs must never leak into sibling imports.
        for name in ("apps.disputes", "apps.disputes.services"):
            sys.modules.pop(name, None)
    assert models._dispute_note_required_for(2, "listings", 1) is False


# ------------------------------------------------------- reputation rollup


def _seed_trust(received_user, voter, votes=6):
    """Give `received_user` a known trust usefulness score. The PUBLIC trust
    scorer over its OWN constants (30 lifetime / 12 recent saturation):
    6 lifetime + 6 recent → 0.6·(6/30) + 0.4·(6/12) = 0.32."""
    from apps.posts import services as posts_services

    for i in range(votes):
        post = posts_services.create_post(
            author=received_user, text=f"Trustable tip number {i}."
        )
        trust_services.record_vote(voter=voter, target=post, now=NOW)


@pytest.mark.django_db
def test_reputation_rollup_math_is_explainable(fresh_user, other_user):
    _seed_trust(other_user, fresh_user)
    for stars in (5, 4, 1):
        listing = listings_services.create_listing(
            seller=other_user,
            title=f"Deal stock {stars}",
            description="One completed deal per star row.",
            category="farm produce",
            ward="Kilimani",
            price=Decimal("100"),
        )
        # Same reviewer, three different deals — unique is per (deal,
        # reviewer), so one member's three deals give three received rows.
        _held_buyer_review(
            fresh_user, listing, stars=stars,
            dispute_note=(
                "Weight was short and quality low." if stars == 1 else ""
            ),
        )

    # Release the pair-ish rows via the sweep (14-day boundary is 20d here —
    # all three are lone drafts; reputation only ever counts PUBLISHED rows).
    svc.publish_due_reviews(now=NOW + timedelta(days=20))

    reputation = svc.reputation_for(other_user, now=NOW)
    # mean(5,4,1) = 10/3 → 3.3333 (4dp); trust usefulness 0.32 (→ 1.6 of 5):
    assert reputation["deal_reviews"]["lifetime"] == 3
    assert reputation["deal_reviews"]["last90"] == 3
    assert reputation["deal_reviews"]["avg_stars"] == 3.3333
    assert reputation["trust"]["score_zero_to_one"] == 0.32
    assert reputation["trust"]["component_out_of_five"] == 1.6
    # score = 0.6·3.3333 + 0.4·1.6 = 2.63998 → 2.64 (documented formula):
    assert reputation["score"] == 2.64
    assert reputation["scale"] == 5
    assert reputation["stars_filled"] == 3
    assert reputation["formula"]


@pytest.mark.django_db
def test_reputation_last90_window_is_clock_injected(fresh_user, other_user):
    stale = listings_services.create_listing(
        seller=other_user, title="Old stock", description="Done long ago.",
        category="farm produce", ward="Kilimani", price=Decimal("80"),
    )
    fresh_listing = listings_services.create_listing(
        seller=other_user, title="Fresh stock", description="Done recently.",
        category="farm produce", ward="Kilimani", price=Decimal("80"),
    )
    _held_buyer_review(fresh_user, stale, stars=1,
                       dispute_note="Never delivered the agreed bunch.")
    _held_buyer_review(fresh_user, fresh_listing, stars=5)
    # Release both through the sweep, then age the stale one past the window.
    svc.publish_due_reviews(now=NOW + timedelta(days=20))
    stale_row = DealReview.objects.get(deal_app="listings", deal_id=stale.pk)
    _shift_created(stale_row, NOW - timedelta(days=91))

    reputation = svc.reputation_for(other_user, now=NOW + timedelta(days=21))
    assert reputation["deal_reviews"]["lifetime"] == 2
    assert reputation["deal_reviews"]["last90"] == 1  # the 91-day-old fell out
    assert reputation["deal_reviews"]["avg_stars"] == 3.0          # lifetime mean
    assert reputation["deal_reviews"]["avg_stars_last90"] == 5.0   # recency trend


@pytest.mark.django_db
def test_reputation_is_deterministic_under_ties(fresh_user, other_user, third_user):
    listing = listings_services.create_listing(
        seller=other_user, title="Tie stock", description="Same-tick rows.",
        category="farm produce", ward="Kilimani", price=Decimal("95"),
    )
    _held_buyer_review(fresh_user, listing, stars=5)
    _held_buyer_review(third_user, listing, stars=4)
    svc.publish_due_reviews(now=NOW + timedelta(days=1))
    first = svc.reputation_for(other_user, now=NOW + timedelta(days=2))
    second = svc.reputation_for(other_user, now=NOW + timedelta(days=2))
    assert first == second  # (created_at, pk) tie-break contract, twice-verified


@pytest.mark.django_db
def test_empty_reputation_is_honest(fresh_user):
    reputation = svc.reputation_for(fresh_user, now=NOW)
    assert reputation["score"] == 0.0
    assert reputation["stars_filled"] == 0
    assert reputation["star_flags"] == [False] * 5
    assert reputation["deal_reviews"]["lifetime"] == 0
    assert reputation["trust"]["score_zero_to_one"] == 0.0


def test_star_flags_round_half_up():
    assert svc.star_flags(0.0) == [False] * 5
    assert svc.star_flags(2.44) == [True, True, False, False, False]
    assert svc.star_flags(2.5) == [True, True, True, False, False]
    assert svc.star_flags(5) == [True] * 5


@pytest.mark.django_db
def test_reputation_partial_renders_and_stays_tiny(fresh_user):
    empty = render_to_string(
        "reviews/_reputation.html", {"reputation": svc.reputation_for(fresh_user, now=NOW)}
    )
    assert "reputation-stars" in empty
    assert_under_budget(empty, 50)


# ------------------------------------------------------------- view flows


@pytest.mark.django_db
def test_pages_require_login():
    client = Client()
    for url in (reverse("reviews:list"), reverse("reviews:leave"), reverse("reviews:my_reviews")):
        assert client.get(url).status_code == 302  # login, never anonymous


@pytest.mark.django_db
def test_list_and_mine_render_the_members_own_rows(
    client_auth, fresh_user, other_user, seller_listing
):
    review = _held_buyer_review(fresh_user, seller_listing, stars=4, text="As promised.")
    assert client_auth.get(reverse("reviews:my_reviews")).status_code == 200
    mine = client_auth.get(reverse("reviews:my_reviews")).content.decode()
    assert "Held" in mine  # honest state on the reviewer's own page
    received_url = reverse("reviews:list")
    reviewer_client = Client()  # the SELLER sees the review they received
    reviewer_client.force_login(other_user)
    received = reviewer_client.get(received_url).content.decode()
    assert review.text not in received  # held: invisible on the recipient side
    svc.publish_due_reviews(now=NOW + timedelta(days=20))
    received = reviewer_client.get(received_url).content.decode()
    assert "As promised." in received and "Held" not in received
    assert_under_budget(received, 50)


@pytest.mark.django_db
def test_leave_view_never_publishes(
    client_auth, fresh_user, other_user, seller_listing
):
    response = client_auth.post(
        reverse("reviews:leave"),
        {
            "deal_app": "listings",
            "deal_id": seller_listing.pk,
            "stars": "4",
            "text": "Solid seller, quick meet.",
        },
        follow=True,
    )
    assert response.status_code == 200
    review = DealReview.objects.get(deal_app="listings", deal_id=seller_listing.pk)
    assert review.status == "DRAFT"  # NO view may publish (the sweep only)
    assert b"counterpart" in response.content and b"14 days" in response.content


@pytest.mark.django_db
def test_leave_view_rejects_bad_input(
    client_auth, fresh_user, other_user, seller_listing
):
    # stars outside 1–5 never reach a row:
    client_auth.post(
        reverse("reviews:leave"),
        {
            "deal_app": "listings", "deal_id": seller_listing.pk,
            "stars": "0", "text": "nope",
        },
    )
    assert DealReview.objects.count() == 0
    # 1-star without the dispute note is refused on the form level:
    response = client_auth.post(
        reverse("reviews:leave"),
        {
            "deal_app": "listings", "deal_id": seller_listing.pk,
            "stars": "1", "dispute_note": "",
        },
    )
    assert DealReview.objects.count() == 0
    assert response.status_code == 200  # re-rendered form with the notice


@pytest.mark.django_db
def test_leave_view_honours_local_next_only(
    client_auth, fresh_user, other_user, seller_listing
):
    response = client_auth.post(
        reverse("reviews:leave"),
        {
            "deal_app": "listings", "deal_id": seller_listing.pk,
            "stars": "3", "text": "ok", "next": "https://evil.example.com/phish",
        },
    )
    assert response.status_code == 302  # redirect, but NOT to the foreign host
    assert b"evil.example.com" not in response["Location"].encode()


@pytest.mark.django_db
def test_bad_get_ref_explains_itself(client_auth, fresh_user):
    # An unreadable ref renders an honest notice — no 404-as-500, no form
    # that lets you think you were reviewing something real.
    response = client_auth.get("/reviews/leave/?deal_app=listings&deal_id=999999")
    assert response.status_code == 200
    assert b"does not exist or is not part of your deals" in response.content


# ------------------------------------------------------------ report flow


@pytest.mark.django_db
def test_reporting_a_published_review_queues_a_report(
    client_auth, fresh_user, other_user, seller_listing, monkeypatch
):
    """Parent consolidation: 'reviews' joined moderation's
    ALLOWED_TARGET_APPS, so a published-review abuse report lands in the
    Prompt 15 queue as a normal OPEN report + audit row. The flow stays
    graceful (message, redirect — never a 500); RateLimitError is caught the
    same way (spec: catch code='report_rate_limited' gracefully)."""
    from apps.moderation.models import AuditAction, ModeratorAudit, Report
    from apps.moderation.services import RateLimitError

    review = _held_buyer_review(fresh_user, seller_listing, stars=3)
    svc.publish_due_reviews(now=NOW + timedelta(days=20))
    review.refresh_from_db()
    assert review.status == "PUBLISHED"

    response = client_auth.post(
        reverse("reviews:report", args=[review.pk]),
        {"reason": "SPAM", "note": "Fake positive reviews."},
    )
    assert response.status_code == 302  # graceful, not a 500
    report = Report.objects.get()
    assert report.target_ct.get_object_for_this_type(pk=report.target_id) == review
    assert report.status == "OPEN"
    assert ModeratorAudit.objects.filter(
        target_ct=report.target_ct, target_id=report.target_id,
        action=AuditAction.REPORT_CREATED,  # TextChoices member coerces to .value
    ).exists()

    # rate-limited variant: the 11th report in the hour is CAUGHT, not raised:
    def _limited(**kwargs):
        raise RateLimitError("hit", code="report_rate_limited")

    monkeypatch.setattr("apps.reviews.views.report_content", _limited)
    limited = client_auth.post(
        reverse("reviews:report", args=[review.pk]), {"reason": "SCAM", "note": ""}
    )
    assert limited.status_code == 302  # warning message, flow survives


@pytest.mark.django_db
def test_report_needs_a_valid_reason(client_auth, fresh_user, other_user, seller_listing):
    review = _held_buyer_review(fresh_user, seller_listing, stars=3)
    svc.publish_due_reviews(now=NOW + timedelta(days=20))
    response = client_auth.post(
        reverse("reviews:report", args=[review.pk]), {"reason": "NOT-A-REASON"}
    )
    assert response.status_code == 302  # refused, not crashed
    assert DealReview.objects.filter(pk=review.pk, status="PUBLISHED").exists()


@pytest.mark.django_db
def test_reports_cannot_target_a_held_review(
    client_auth, fresh_user, other_user, seller_listing
):
    review = _held_buyer_review(fresh_user, seller_listing, stars=3)
    response = client_auth.post(
        reverse("reviews:report", args=[review.pk]), {"reason": "SPAM"}
    )
    assert response.status_code == 404  # nothing published: nothing to abuse


# ------------------------------------------------------------------ i18n


@pytest.mark.django_db
def test_pages_render_under_sw_locale(client_auth, fresh_user):
    """gettext-marked strings must switch locale without crashing (catalogs
    may lag; msgid fallback keeps pages honest + inside budget)."""
    from django.utils import translation

    with translation.override("sw"):
        for url in (reverse("reviews:list"), reverse("reviews:my_reviews"), reverse("reviews:leave")):
            response = client_auth.get(url)
            assert response.status_code == 200
        assert_under_budget(response.content.decode(), 50)
