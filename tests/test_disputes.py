"""Disputes & fraud prevention tests (Prompt 23 — apps/disputes).

Coverage map (PROMPTS.md Prompt 23 test contract):
- case lifecycle: open -> under review -> human decision, evidence intact
- one OPEN case per deal (unique), honest re-open after a decision
- evidence linkage: barter lineage + enquiry-thread messages auto-linked
  BY ID (no content copied), image + text attachments, ownership gates
- proposal -> confirm flow with the two-actor refusal (same human refused)
- fraud heuristics flag fixture patterns AND no false flags on benign
  similar produce; flag_scan is annotation-only (nothing auto-acts)
- dispute_open_for_deal binds reviews' 1-star gate (True open, False after
  resolve AND dismiss) — the Prompt 22 contract
- aging tiers via an injected clock; NO auto-close anywhere
- page budgets, model-level consistency, lazy (translatable) messages
"""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.functional import Promise

from apps.core.testing import mock_now

import apps.disputes.services as ds
from apps.disputes.models import (
    CaseStatus,
    CaseTier,
    DisputeCase,
    EvidenceAttachment,
    EvidenceKind,
    FraudFlag,
    ProposalAction,
)

User = get_user_model()


# ----------------------------------------------------------------- fixtures


@pytest.fixture
def opener(db):
    return User.objects.create_user(
        username="awamu", email="awamu@example.com", password="***"
    )


@pytest.fixture
def accused(db):
    return User.objects.create_user(
        username="birungi", email="birungi@example.com", password="***"
    )


@pytest.fixture
def stranger(db):
    return User.objects.create_user(
        username="kato", email="kato@example.com", password="***"
    )


@pytest.fixture
def mod(db):
    user = User.objects.create_user(
        username="mod_one", email="mod1@example.com", password="***"
    )
    user.is_staff = True
    user.save()
    return user


@pytest.fixture
def mod_two(db):
    user = User.objects.create_user(
        username="mod_two", email="mod2@example.com", password="***"
    )
    user.is_staff = True
    user.save()
    return user


def _sent_cash_deal(initiator, counterpart, price=Decimal("500")):
    """A completed single-version CASH deal; returns the ROOT row."""
    from apps.barter import services as barter

    offer = barter.create_offer(
        initiator=initiator,
        counterpart=counterpart,
        mode="CASH",
        cash_amount=price,
    )
    barter.send_offer(offer, by_user=initiator)
    barter.accept(offer, by_user=counterpart)
    barter.complete(offer, by_user=initiator)
    return offer.walk_to_root()


def _countered_deal(initiator, counterpart):
    """A completed TWO-version lineage (root + a counter); returns
    (root, child_tip) — the disputed-id normalization fixture."""
    from apps.barter import services as barter

    root = barter.create_offer(
        initiator=initiator, counterpart=counterpart,
        mode="CASH", cash_amount=Decimal("500"),
    )
    barter.send_offer(root, by_user=initiator)
    counter = barter.create_counteroffer(
        parent=root, counterer=counterpart,
        mode="CASH", cash_amount=Decimal("400"),
    )
    tip = barter.send_offer(counter, by_user=counterpart)
    barter.accept(tip, by_user=initiator)
    barter.complete(tip, by_user=counterpart)
    return root, tip


def _published_listing(seller, title="Fresh sukuma wiki", description="Green bunches harvested this morning.", price=Decimal("50")):
    from apps.listings.services import create_listing

    return create_listing(
        seller=seller,
        title=title,
        description=description,
        category="farm produce",
        ward="Kilimani",
        price=price,
    )


def _dispute(opener, deal_app, deal_id, **kw):
    return ds.open_case(
        opener=opener,
        deal_app=deal_app,
        deal_id=deal_id,
        what_promised=kw.get("what_promised", "Two bunches of sukuma wiki."),
        what_happened=kw.get("what_happened", "I paid but nothing was delivered."),
        now=kw.get("now"),
    )


# ------------------------------------------------------- open case + gates


@pytest.mark.django_db
class TestCaseOpening:
    def test_barter_open_normalizes_to_root_and_links_lineage(
        self, opener, accused
    ):
        """Deal opened with a CHILD version id stores the ROOT id; every
        sent lineage version arrives as id-only MESSAGE evidence."""
        root, tip = _countered_deal(opener, accused)

        case = _dispute(
            opener, "barter", tip.pk,
            what_promised="Solar lamp for 2kg maize.",
            what_happened="They kept the lamp; no transfer happened.",
            now=mock_now(),
        )
        assert case.deal_id == root.pk  # normalized to the lineage root
        evidence = list(case.evidence.all())
        assert {row.offer_id for row in evidence} == {root.pk, tip.pk}
        assert all(row.kind == EvidenceKind.MESSAGE.value for row in evidence)
        # KDPA: ids only — nothing content-shaped on the rows.
        assert not any(row.text for row in evidence)
        assert not any(row.thread_id or row.message_id for row in evidence)

    def test_non_party_gets_the_same_refusal_as_a_missing_deal(
        self, opener, stranger
    ):
        root = _sent_cash_deal(opener, stranger)
        other_party = User.objects.create_user(
            username="mtumgeni", email="mg@example.com", password="***"
        )
        with pytest.raises(ValidationError) as exc:
            _dispute(other_party, "barter", root.pk)
        assert str(ds.MSG_BAD_DEAL) in [str(m) for m in exc.value.messages]
        assert DisputeCase.objects.count() == 0

    def test_requires_sent_terms_and_allowed_origin(self, opener, accused):
        from apps.barter import services as barter

        draft = barter.create_offer(
            initiator=opener, counterpart=accused, mode="CASH",
            cash_amount=Decimal("500"),
        )
        with pytest.raises(ValidationError) as draft_exc:
            _dispute(opener, "barter", draft.pk)
        assert str(ds.MSG_DRAFT_ONLY) in [str(m) for m in draft_exc.value.messages]

        with pytest.raises(ValidationError) as app_exc:
            _dispute(opener, "posts", 1)
        assert ds.MSG_BAD_APP in [  # lazy constants compare by str
            str(m) for m in app_exc.value.messages
        ]

    def test_bare_claim_is_refused(self, opener, accused):
        root = _sent_cash_deal(opener, accused)
        with pytest.raises(ValidationError) as exc:
            ds.open_case(
                opener=opener, deal_app="barter", deal_id=root.pk,
                what_promised="  ", what_happened="", now=mock_now(),
            )
        assert [str(m) for m in exc.value.messages]  # an honest refusal exists
        assert DisputeCase.objects.count() == 0

    def test_one_open_case_per_deal_then_reopen_after_decision(
        self, opener, accused, mod
    ):
        root = _sent_cash_deal(opener, accused)
        first = _dispute(opener, "barter", root.pk, now=mock_now())
        with pytest.raises(ValidationError) as exc:
            _dispute(accused, "barter", root.pk, now=mock_now())
        assert ds.MSG_ALREADY_OPEN in [str(m) for m in exc.value.messages]

        ds.resolve_case(first, moderator=mod, now=mock_now())
        second = _dispute(accused, "barter", root.pk, now=mock_now())
        assert second.pk != first.pk  # decided case does not block re-opening


# --------------------------------------------------- lifecycle & evidence


@pytest.mark.django_db
class TestLifecycleAndEvidence:
    def test_fake_deal_reaches_human_decision_with_evidence_intact(
        self, opener, accused, mod, mod_two
    ):
        """THE acceptance test: a fake-deal dispute ends at a confirmed
        human decision, and every evidence row survives untouched."""
        listing = _published_listing(accused)  # the scammer's fake listing
        from apps.messaging import services as messaging

        thread = messaging.create_enquiry_thread(
            initiator=opener, listing_id=listing.pk
        )
        messaging.send_message(
            thread=thread, sender=opener, body="Paying for it now."
        )
        messaging.send_message(
            thread=thread, sender=accused, body="Send M-Pesa to 0712345678."
        )
        case = _dispute(
            opener, "listings", listing.pk,
            what_happened="Sellers phone number led off-platform; no item arrived.",
            now=mock_now(),
        )
        assert case.status == CaseStatus.OPEN.value
        before_evidence = list(case.evidence.all())  # auto-linked chat
        assert len(before_evidence) >= 1

        ds.start_review(case, moderator=mod, now=mock_now())
        assert case.status == CaseStatus.UNDER_REVIEW.value

        proposal = ds.propose_outcome(
            case, proposer=mod, action="REMOVE_LISTING",
            note="Off-platform payment solicitation.", now=mock_now(),
        )
        assert proposal.confirmed_by_id is None
        confirmed = ds.confirm_proposal(
            list(case.proposals.all())[-1],
            confirmer=mod_two,
            decision_note="Confirmed removal.",
            now=mock_now(),
        )
        assert confirmed.confirmed_by_id == mod_two.pk
        listing.refresh_from_db()
        assert listing.status == "REMOVED"
        case.refresh_from_db()
        assert case.status == CaseStatus.RESOLVED.value
        assert case.outcome == ProposalAction.REMOVE_LISTING.value
        after_evidence = list(case.evidence.all())
        assert after_evidence == before_evidence  # evidence retained, ids equal

    def test_listing_dispute_autolinks_the_enquiry_thread(
        self, opener, accused
    ):
        """IDs only: the negotiation thread lands as MESSAGE rows — bodies
        and phone/email names are NEVER copied into the disputes app."""
        from apps.messaging import services as messaging

        listing = _published_listing(accused)
        thread = messaging.create_enquiry_thread(
            initiator=opener, listing_id=listing.pk
        )
        messaging.send_message(
            thread=thread, sender=opener, body="Is it available today?"
        )
        messaging.send_message(
            thread=thread, sender=accused, body="Yes. Send M-Pesa to 0712345678."
        )
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        rows = list(case.evidence.filter(kind="MESSAGE"))
        assert len(rows) == 2
        assert all(row.thread_id == thread.pk for row in rows)
        joined = " ".join(str(row) for row in rows) + " ".join(
            str(getattr(row, "text", "")) for row in rows
        )
        assert "0712345678" not in joined  # KDPA: digits never copied
        assert "available" not in joined   # bodies never copied

    def test_evidence_ownership_and_mod_gathering(self, opener, accused, mod):
        from apps.media.models import MediaRef

        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        their_ref = MediaRef.objects.create(
            owner=accused, purpose="listing", standard_path="s/1.jpg",
            standard_bytes=1000, standard_w=100, standard_h=100,
            thumb_path="t/1.jpg", thumb_bytes=100, thumb_w=10, thumb_h=10,
            original_bytes=2000, original_format="jpg",
        )
        with pytest.raises(ValidationError) as own_exc:
            ds.attach_image_evidence(case=case, added_by=opener, image=their_ref)
        assert ds.MSG_NOT_YOUR_IMAGE in [str(m) for m in own_exc.value.messages]

        mine_ref = MediaRef.objects.create(
            owner=opener, purpose="message", standard_path="s/2.jpg",
            standard_bytes=1000, standard_w=100, standard_h=100,
            thumb_path="t/2.jpg", thumb_bytes=100, thumb_w=10, thumb_h=10,
            original_bytes=2000, original_format="jpg",
        )
        row = ds.attach_image_evidence(case=case, added_by=opener, image=mine_ref)
        assert row.image_ref_id == mine_ref.pk
        assert row.kind == EvidenceKind.IMAGE.value
        with pytest.raises(ValidationError):
            ds.attach_image_evidence(case=case, added_by=opener, image=mine_ref)

        ds.attach_image_evidence(case=case, added_by=mod, image=their_ref)

    def test_non_party_cannot_attach_evidence(self, opener, accused, stranger):
        root = _sent_cash_deal(opener, accused)
        case = _dispute(opener, "barter", root.pk, now=mock_now())
        with pytest.raises(ValidationError) as exc:
            ds.attach_text_evidence(
                case=case, added_by=stranger, text="I was here too."
            )
        assert ds.MSG_NOT_A_PARTY in [str(m) for m in exc.value.messages]

    def test_evidence_kind_consistency(self, opener, accused):
        root = _sent_cash_deal(opener, accused)
        case = _dispute(opener, "barter", root.pk, now=mock_now())
        case_pk = case.pk
        bad_message = EvidenceAttachment(
            case_id=case_pk, added_by=opener,
            kind=EvidenceKind.MESSAGE.value,
            thread_id=None, message_id=None, offer_id=None,
        )
        with pytest.raises(ValidationError):
            bad_message.full_clean()
        bad_image = EvidenceAttachment(
            case_id=case_pk, added_by=opener,
            kind=EvidenceKind.IMAGE.value, message_id=9,
        )
        with pytest.raises(ValidationError):
            bad_image.full_clean()
        bad_text = EvidenceAttachment(
            case_id=case_pk, added_by=opener,
            kind=EvidenceKind.TEXT.value, message_id=9, text="note",
        )
        with pytest.raises(ValidationError):
            bad_text.full_clean()


# ---------------------------------------------------- proposals & the gate


@pytest.mark.django_db
class TestHumanGate:
    def test_propose_picks_case_up_and_labels_the_accused(
        self, opener, accused, mod
    ):
        listing = _published_listing(opener)  # opener sells; accused buys
        case = _dispute(accused, "listings", listing.pk, now=mock_now())
        proposal = ds.propose_outcome(
            case, proposer=mod, action="WARN", note="First warning.", now=mock_now()
        )
        assert proposal.proposed_by_id == mod.pk
        assert proposal.confirmed_by_id is None
        assert proposal.target is not None
        assert proposal.target.pk == opener.pk  # the accused member
        case.refresh_from_db()
        assert case.status == CaseStatus.UNDER_REVIEW.value
        # NOTHING executed yet — the gate is the proposal, not the proposal.
        opener.refresh_from_db()
        assert opener.is_active is True
        assert listing.status == "PUBLISHED"

    def test_same_human_cannot_confirm_own_proposal(
        self, opener, accused, mod
    ):
        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        ds.propose_outcome(
            case, proposer=mod, action="REMOVE_LISTING", note="", now=mock_now()
        )
        proposal = list(case.proposals.all())[-1]
        with pytest.raises(ValidationError) as exc:
            ds.confirm_proposal(proposal, confirmer=mod, now=mock_now())
        messages = [str(m) for m in exc.value.messages]
        assert str(ds.MSG_TWO_ACTORS) in messages
        # Refusal BEFORE any execution, and the case stays open-flow.
        opener.refresh_from_db()
        listing.refresh_from_db()
        assert listing.status == "PUBLISHED"
        case.refresh_from_db()
        assert case.status == CaseStatus.UNDER_REVIEW.value
        assert proposal.is_confirmed is False

    def test_suspend_confirms_through_moderations_two_actor_gate(
        self, opener, accused, mod, mod_two
    ):
        from apps.moderation.models import (
            ModeratorAudit,
            SuspensionDraft,
            SuspensionStatus,
        )

        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        ds.propose_outcome(
            case, proposer=mod, action="SUSPEND",
            note="Confirmed serial scammer.", now=mock_now(),
        )
        proposal = list(case.proposals.all())[-1]
        ds.confirm_proposal(
            proposal, confirmer=mod_two, now=mock_now()
        )
        accused.refresh_from_db()
        assert accused.is_active is False
        draft = SuspensionDraft.objects.get(target=accused)
        assert draft.drafted_by_id == mod.pk           # gate step 1
        assert draft.confirmed_by_id == mod_two.pk     # gate step 2
        assert draft.status == SuspensionStatus.CONFIRMED.value
        actions = set(
            ModeratorAudit.objects.filter(target_id=accused.pk).values_list(
                "action", flat=True
            )
        )
        assert {"SUSPENSION_DRAFTED", "SUSPENSION_CONFIRMED"} <= actions
        # A warned member still warns under a removed case listing flow:
        opener.refresh_from_db()
        assert opener.is_active is True

    def test_warn_and_mark_review_leave_audit_rows_not_bans(
        self, opener, accused, mod, mod_two
    ):
        from apps.moderation.models import ModeratorAudit

        listing = _published_listing(accused)
        warn_case = _dispute(
            opener, "listings", listing.pk,
            what_promised="Friendly trade, two bunches.",
            what_happened="Kept messaging me outside the app.",
            now=mock_now(),
        )
        ds.propose_outcome(warn_case, proposer=mod, action="WARN", note="", now=mock_now())
        ds.confirm_proposal(
            list(warn_case.proposals.all())[-1], confirmer=mod_two, now=mock_now()
        )
        assert ModeratorAudit.objects.filter(action=ds.AUDIT_WARN).count() == 1
        accused.refresh_from_db()
        assert accused.is_active is True  # a warn is not a suspension

        listing2 = _published_listing(accused, title="Solar lamp", description="Portable lamp")
        review_case = _dispute(
            opener, "listings", listing2.pk,
            what_happened="Described features it does not have.",
            now=mock_now(),
        )
        ds.propose_outcome(
            review_case, proposer=mod_two, action="MARK_REVIEW", note="", now=mock_now()
        )
        ds.confirm_proposal(
            list(review_case.proposals.all())[-1], confirmer=mod, now=mock_now()
        )
        assert ModeratorAudit.objects.filter(action=ds.AUDIT_MARK_REVIEW).count() == 1
        listing2.refresh_from_db()
        assert listing2.review_flagged is True
        assert listing2.status == "PUBLISHED"  # marked, not removed

    def test_queue_is_moderator_only(self, client, opener):
        client.force_login(opener)
        response = client.get("/disputes/queue/")
        assert response.status_code == 403

    def test_admin_confirm_flow_through_views(
        self, opener, accused, mod, mod_two
    ):
        """Full view-level flow: propose (mod 1) -> confirm (mod 2) via the
        routes — the spec's 'admin confirm flow'."""
        from django.test import Client

        listing = _published_listing(accused)
        c_opener, c_mod, c_mod2 = Client(), Client(), Client()
        c_opener.force_login(opener)
        c_mod.force_login(mod)
        c_mod2.force_login(mod_two)

        response = c_opener.post(
            "/disputes/open/",
            {
                "deal_app": "listings",
                "deal_id": listing.pk,
                "what_promised": "Solar lamp, 10W panel.",
                "what_happened": "Payment sent; seller went silent.",
            },
        )
        assert response.status_code == 302
        case = DisputeCase.objects.order_by("-pk").first()
        assert case.deal_id == listing.pk and case.status == CaseStatus.OPEN.value

        c_mod.post(
            f"/disputes/{case.pk}/propose/",
            {"action": "REMOVE_LISTING", "note": "Unreachable seller."},
        )
        case.refresh_from_db()
        assert case.proposals.count() == 1
        assert case.status == CaseStatus.UNDER_REVIEW.value
        response = c_mod2.post(
            f"/disputes/{case.pk}/confirm/",
            {"proposal": list(case.proposals.all())[-1].pk},
        )
        assert response.status_code == 302
        listing.refresh_from_db()
        assert listing.status == "REMOVED"
        case.refresh_from_db()
        assert case.status == CaseStatus.RESOLVED.value


# ------------------------------------------------------- fraud heuristics


@pytest.mark.django_db
class TestFraudHeuristics:
    def test_duplicate_text_flags_across_accounts_not_one_seller(
        self, opener, accused
    ):
        """Same text, two sellers -> both listings flagged. ONE seller with
        two identical listings -> bookkeeping, NOT flagged (no false ban of
        a farmer restating their own goods)."""
        text = ("Kikapu title", "Exact same long description text here.")
        _published_listing(opener, title=text[0], description=text[1])
        _published_listing(accused, title=text[0], description=text[1])
        _published_listing(
            opener, title="Second restatement", description="Exact same long description text here."
        )
        created = ds.flag_scan(now=mock_now())
        assert created == 2
        assert FraudFlag.objects.filter(heuristic="DUPLICATE_TEXT").count() == 2

    def test_benign_similar_produce_is_never_flagged(self, opener, accused):
        """THE pitfall: two neighbours selling the same vegetable with
        similar-but-not-identical texts are NOT fraud."""
        _published_listing(
            opener, title="Sukuma wiki fresh", description="Cut from my garden at dawn."
        )
        _published_listing(
            accused, title="Sukuma wiki fresh 2", description="From the Kibera garden."
        )
        assert ds.flag_scan(now=mock_now()) == 0
        assert FraudFlag.objects.count() == 0

    def test_new_account_bulk_flags_the_young_seller(self, opener, accused):
        from django.utils import timezone
        from datetime import timedelta

        now = mock_now()
        young, _ = User.objects.get_or_create(username="young")
        young.date_joined = now - timedelta(days=2)
        young.save()
        older, _ = User.objects.get_or_create(username="older")
        older.date_joined = now - timedelta(days=90)
        older.save()
        recent = now - timedelta(hours=1)
        import django.utils.timezone as tz

        for user in (young, older):
            for i in range(6):
                listing = _published_listing(
                    user, title=f"Bulk item {user}-{i}",
                    description=f"Item {i} bulk lot text."
                )
                listing.created_at = recent
                listing.save(update_fields=["created_at"])
        created = ds.flag_scan(now=now)
        assert created == 1
        flag = FraudFlag.objects.get(heuristic="NEW_ACCOUNT_BULK")
        assert flag.target_ct.app_label == "accounts"
        assert flag.target_id == young.pk
        assert "6" in flag.note and "day" in flag.note
        # nothing auto-acted:
        young.refresh_from_db()
        assert young.is_active is True

    def test_off_platform_pay_flags_phrases_and_or_shapes(self, opener):
        naive = _published_listing(
            opener, title="Cheap phone", description="Brand new. Send M-Pesa to 0712345678."
        )
        clean = _published_listing(
            opener, title="Quiet title here", description="Priced in-app, chat anytime."
        )
        assert ds.flag_scan() >= 1
        flags = [
            flag for flag in FraudFlag.objects.all()
            if flag.heuristic == "OFF_PLATFORM_PAY" and flag.target_ct.app_label == "listings"
        ]
        assert len(flags) in (1,)  # exactly the flagged listing
        assert flags[0].target_id == naive.pk
        note = flags[0].note
        assert "send m-pesa" in note
        assert "0712345678" not in note  # KDPA: no phone digits in rows
        assert clean.status == "PUBLISHED"  # annotation, not moderation

    def test_flag_scan_is_annotation_only(self, opener, accused):
        """THE pitfall, code-level: flag_scan must not move ANY status,
        review_flag, is_active, suspension draft or audit row."""
        from apps.moderation.models import ModeratorAudit, SuspensionDraft

        listing = _published_listing(
            accused, title="Identical fake", description="Identical fake description."
        )
        opener_listing = _published_listing(
            opener, title="Identical fake", description="Identical fake description."
        )
        assert ds.flag_scan(now=mock_now()) == 2
        listing.refresh_from_db()
        opener_listing.refresh_from_db()
        accused.refresh_from_db()
        # state untouched by the scan:
        assert listing.status == "PUBLISHED" and listing.review_flagged is False
        assert opener_listing.review_flagged is False
        assert accused.is_active is True and opener.is_active is True
        assert SuspensionDraft.objects.count() == 0
        # moderator-authored audit rows only come from the human gate —
        # list creation makes none; the scan adds nothing either.
        assert not ModeratorAudit.objects.exists()

    def test_flag_scan_is_idempotent(self, opener, accused):
        _published_listing(
            opener, title="Same text", description="Same text description."
        )
        _published_listing(
            accused, title="Same text", description="Same text description."
        )
        assert ds.flag_scan(now=mock_now()) == 2
        assert ds.flag_scan(now=mock_now()) == 0


# ------------------------------------------------------- reviews + aging


@pytest.mark.django_db
class TestGateBindingAndAging:
    def test_dispute_open_for_deal_binds_the_reviews_one_star_gate(
        self, opener, accused, mod
    ):
        """The Prompt 22 contract, end to end: while a dispute is OPEN the
        1-star note is required; resolved OR dismissed -> not required."""
        from apps.reviews import services as reviews_services

        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        assert ds.dispute_open_for_deal("listings", listing.pk) is True
        with pytest.raises(ValidationError):
            reviews_services.create_deal_review(
                reviewer=opener, deal_app="listings", deal_id=listing.pk,
                stars=1, text="", dispute_note="too short.",
            )
        ds.resolve_case(case, moderator=mod, now=mock_now())
        assert ds.dispute_open_for_deal("listings", listing.pk) is False
        held = reviews_services.create_deal_review(
            reviewer=opener, deal_app="listings", deal_id=listing.pk,
            stars=1, text="", dispute_note="still short — accepted now",
        )
        assert held.status == "DRAFT"
        # An unknown/deal-less ref never crashes the gate:
        assert ds.dispute_open_for_deal("listings", 98765) is False
        assert ds.dispute_open_for_deal("posts", 1) is False

    def test_dismissed_dispute_releases_the_gate_too(
        self, opener, accused, mod
    ):
        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        ds.dismiss_case(case, moderator=mod, now=mock_now())
        assert ds.dispute_open_for_deal("listings", listing.pk) is False

    def test_aging_tiers_with_the_injected_clock(self, opener, accused):
        from datetime import timedelta

        now = mock_now()
        case = _dispute(
            opener, "barter", _sent_cash_deal(opener, accused).pk, now=now
        )
        assert ds.aging_tier(case, now + timedelta(days=1)) == CaseTier.NORMAL.value
        # exactly 3d -> attention (band edges inclusive per spec bands)
        assert ds.aging_tier(case, now + timedelta(days=3)) == CaseTier.ATTENTION.value
        assert ds.aging_tier(case, now + timedelta(days=6)) == CaseTier.ATTENTION.value
        # exactly 7d -> escalated
        assert ds.aging_tier(case, now + timedelta(days=7)) == CaseTier.ESCALATED.value
        assert [c.pk for c in ds.escalated_cases(now + timedelta(days=8))] == [case.pk]
        assert case.status not in (
            CaseStatus.RESOLVED.value,
            CaseStatus.DISMISSED.value,
        )

    def test_cases_never_silently_close(self, opener, accused):
        from datetime import timedelta

        month_ago = mock_now() - timedelta(days=30)
        case = _dispute(
            opener, "barter", _sent_cash_deal(opener, accused).pk, now=month_ago
        )
        assert ds.flag_scan(now=mock_now()) == 0  # scans change nothing here
        assert case.status == CaseStatus.OPEN.value  # nothing closed it
        assert ds.aging_tier(case, mock_now()) == CaseTier.ESCALATED.value
        assert len(ds.escalated_cases(mock_now())) == 1

    def test_aging_message_is_lazy_translateable(self):
        # i18n hard rule: user-facing service strings are lazy translatable.
        assert isinstance(ds.MSG_ALREADY_OPEN, Promise)
        assert isinstance(ds.MSG_TWO_ACTORS, Promise)


# ------------------------------------------------------- pages & budgets


@pytest.mark.django_db
class TestPages:
    def test_queue_and_case_detail_under_budget(
        self, client, opener, accused, mod
    ):
        from apps.core.testing import assert_under_budget

        listing = _published_listing(accused)
        case = _dispute(
            opener, "listings", listing.pk,
            what_happened="They took the deposit without shipping.",
            now=mock_now(),
        )
        client.force_login(mod)
        queue_page = client.get("/disputes/queue/")
        assert queue_page.status_code == 200
        assert_under_budget(queue_page.content.decode(), 50)
        detail_page = client.get(f"/disputes/{case.pk}/")
        assert detail_page.status_code == 200
        assert_under_budget(detail_page.content.decode(), 50)
        client.force_login(opener)
        mine = client.get("/disputes/mine/")
        assert mine.status_code == 200
        assert_under_budget(mine.content.decode(), 50)

    def test_stranger_gets_missing_case_404(self, opener, accused):
        from django.test import Client

        listing = _published_listing(accused)
        case = _dispute(opener, "listings", listing.pk, now=mock_now())
        stranger = User.objects.create_user(
            username="nje", email="nje@example.com", password="***"
        )
        c_stranger = Client()
        c_stranger.force_login(stranger)
        # IDOR shape: a stranger reads a case like a missing one.
        assert c_stranger.get(f"/disputes/{case.pk}/").status_code == 404
