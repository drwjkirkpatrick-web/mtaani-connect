"""Disputes services (Prompt 23) — one door for every write + shared reads.

PUBLIC API (what sibling apps and views use — never reach past this):
- dispute_open_for_deal(deal_app, deal_id) -> bool
    THE reviews binding contract (Prompt 22's apps/reviews/models.py): a
    1-star review REQUIRES a dispute note while a dispute is OPEN on the
    deal. Implemented EXACTLY at this signature; reviews imports it lazily.
- open_case / attach_text_evidence / attach_image_evidence
- start_review / propose_outcome / confirm_proposal / resolve_case /
  dismiss_case
- case_age / aging_tier / queue_cases / escalated_cases
- flag_scan(now=None) -> int
- cases_for(user) / case_label(case) / evidence_rows_for(case, viewer)

HUMAN REVIEW GATE (Prompt 23 + Hard Rules): open_case, flags and aging are
system/member steps. NOTHING consequential (warn/suspend/remove/mark-review)
happens on a proposal — it happens on CONFIRM, by a DIFFERENT human, and
even then only through moderation's PUBLIC machinery:

    SUSPEND        -> moderation.draft_suspension (by the PROPOSER, step 1)
                      + moderation.confirm_suspension (by the CONFIRMER);
                      moderation writes both SUSPENSION_DRAFTED and
                      SUSPENSION_CONFIRMED audit rows with the two actors.
    REMOVE_LISTING -> moderation.apply_removal(target) + a ModeratorAudit
                      REMOVED row (moderation's own vocabulary here).
    WARN / MARK_REVIEW -> an immutable ModeratorAudit row in the same
                      KDPA-minimum shape, action VALUE "DISPUTE_WARN" /
                      "DISPUTE_MARK_REVIEW" (moderation's Prompt-15 action
                      vocabulary has no warn word yet; Prompt 37 extends
                      that enum — until then these opaque values ride the
                      same row shape, no parallel tables).

FRAUD HEURISTICS ARE ANNOTATION-ONLY (the named pitfall: auto-suspending on
heuristics would ban legitimate farmers selling similar produce — two
neighbours legitimately list the same vegetables):
flag_scan() creates FraudFlag rows and NOTHING else — no status, no
suspension, no removal, no audit, no review_flag. Humans decide.

NO AUTO-CLOSE ANYWHERE: aging only annotates tiers for the dashboard. A
case open for 300 days is still OPEN; only a human closes it.

INJECTED CLOCK: every time-aware function takes `now=None` (the hard rule
that time-dependent logic is deterministic in tests).
"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    ALLOWED_DEAL_APPS,
    MAX_DECISION_NOTE_CHARS,
    CaseStatus,
    CaseTier,
    DisputeCase,
    DisputeProposal,
    EvidenceAttachment,
    EvidenceKind,
    FraudFlag,
    FraudHeuristic,
    ProposalAction,
)

# ---------------------------------------------------------------- aging ----
# Spec verbatim: unresolved cases escalate in the admin dashboard, never
# silently close. Tier bands are INCLUSIVE at the upper edge of the next
# band: exactly 3 days old == attention, exactly 7 days == escalated.
AGE_NORMAL_DAYS = 3
AGE_ESCALATED_DAYS = 7

# Auto-linked evidence window (the 50 KB mobile rule applies to the case
# detail page — window the negotiation trail, never dump everything).
EVIDENCE_MESSAGE_WINDOW = 20

OPEN = CaseStatus.OPEN.value
UNDER_REVIEW = CaseStatus.UNDER_REVIEW.value
RESOLVED = CaseStatus.RESOLVED.value
DISMISSED = CaseStatus.DISMISSED.value
OPEN_FLOW = (OPEN, UNDER_REVIEW)

MSG_LOGIN = _("Log in to use dispute cases.")
MSG_BAD_APP = _("Disputes exist for barter, marketplace and service deals only.")
MSG_BAD_DEAL = _(
    "That deal does not exist or you are not disputing on it — ask the other "
    "party, or report the content instead."
)
MSG_DRAFT_ONLY = _(
    "That negotiation has no sent terms yet — send it before disputing it."
)
MSG_ALREADY_OPEN = _("This deal already has an open dispute case.")
MSG_CASE_CLOSED = _("This case is decided — nothing can change it now.")
MSG_ONLY_MODERATORS = _("Only moderators administer dispute cases.")
MSG_NOT_A_PARTY = _(
    "Only the parties of this deal or a moderator may work on the case."
)
MSG_TWO_ACTORS = _(
    "A proposed outcome must be confirmed by a different moderator — the "
    "human gate is code, not culture."
)
MSG_ALREADY_CONFIRMED = _("That proposal is already confirmed.")
MSG_NO_COUNTERPARTY = _(
    "No identifiable counterparty member on this deal — marketplace listings "
    "record the seller only, so warn/suspend needs them as the accused."
)
MSG_NO_LINKED_LISTING = _(
    "This negotiation never referenced a marketplace listing — remove-listing "
    "does not apply. Warn, suspend or dismiss instead."
)
MSG_EMPTY_TEXT = _("Write the note the reviewer should read.")
MSG_EMPTY_IMAGE = _("Pick an uploaded image to attach.")
MSG_NOT_YOUR_IMAGE = _("That image belongs to another member.")
MSG_ALREADY_EVIDENCE = _("That evidence is already attached to this case.")

# Moderator-only audit action VALUES for outcomes moderation's Prompt-15
# vocabulary cannot name yet (see module docstring; Prompt 37 extends it).
AUDIT_WARN = "DISPUTE_WARN"
AUDIT_MARK_REVIEW = "DISPUTE_MARK_REVIEW"


def _now(now):
    return timezone.now() if now is None else now


def _require_auth(user):
    if user is None or not getattr(user, "is_authenticated", False):
        raise ValidationError(MSG_LOGIN)


# ----------------------------------------------------------- deal reads ---
# Party resolution follows Prompt 22's precedent (apps/reviews/services.py
# _resolve_subject): each origin through ITS public door, never sibling
# model internals.


def _barter_root(deal_id, for_user=None):
    """The Offer row for a barter deal id (any version or the root).

    Public doors only: services.get_offer_for (party-gated, sent rules
    included) when a member is given; services._root_row — the id-only
    door barter added as its Prompt 22/23 cross-app contract — for
    moderator/id-only reads. None == absent (never fabricated).
    """
    from apps.barter import services as barter_services

    if for_user is not None:
        return barter_services.get_offer_for(for_user, deal_id)
    door = getattr(barter_services, "_root_row", None)
    if callable(door):
        try:
            return door(deal_id)
        except Exception:  # noqa: BLE001 — a broken door is honest absence
            return None
    return None


def _barter_services():
    from apps.barter import services as barter_services

    return barter_services


def deal_parties_ids(deal_app: str, deal_id, for_user=None) -> set[int]:
    """The member ids recorded on the deal itself (opener-independent).

    barter: the root's two parties. listings: the seller (Prompt 17 has no
    buyer identity — reviews documented this same limitation; Prompt 24 may
    add deal records and only this function changes). services: provider.
    """
    try:
        deal_id = int(deal_id)
    except (TypeError, ValueError):
        return set()
    if deal_app == "barter":
        root = _barter_root(deal_id, for_user)
        if root is None:
            return set()
        return {root.initiator_id, root.counterpart_id}
    if deal_app == "listings":
        from apps.listings.services import get_visible_listing

        listing = get_visible_listing(deal_id, for_user)
        return {listing.seller_id} if listing is not None else set()
    if deal_app == "services":
        from apps.services.services import get_visible_service

        service = get_visible_service(deal_id, for_user)
        return {service.provider_id} if service is not None else set()
    return set()


def _deal_readable(deal_app: str, deal_id, user) -> bool:
    """Can `user` legitimately dispute this deal? barter: being a party
    (the door returns None for strangers AND for other members' drafts);
    listings/services: the deal visible through the sibling's public read
    door (seller's own, staff, or PUBLISHED)."""
    if deal_app == "barter":
        return _barter_root(deal_id, user) is not None
    if deal_app == "listings":
        from apps.listings.services import get_visible_listing

        try:
            return get_visible_listing(int(deal_id), user) is not None
        except (TypeError, ValueError):
            return False
    if deal_app == "services":
        from apps.services.services import get_visible_service

        try:
            return get_visible_service(int(deal_id), user) is not None
        except (TypeError, ValueError):
            return False
    return False


def can_access_case(case: DisputeCase, user) -> bool:
    """Case-detail access: the opener, a deal party, or a moderator.
    Anonymous users are refused upstream (login_required); this answers
    the content question only, via each origin's party rule."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if case.opener_id == user.pk:
        return True
    from apps.moderation.services import is_moderator

    if is_moderator(user):
        return True
    return user.pk in deal_parties_ids(case.deal_app, case.deal_id, for_user=user)


def case_label(case: DisputeCase) -> str:
    """A short, honest deal label for lists/queues (per-origin label doors,
    'Deal #<id>' fallback — never a fabricated title)."""
    deal_id = case.deal_id
    if case.deal_app == "barter":
        label = _barter_services().deal_label(deal_id)
        return label or f"Deal #{deal_id}"
    if case.deal_app == "listings":
        from apps.listings.services import get_visible_listing

        listing = get_visible_listing(deal_id, None)  # PUBLISHED-only label
        if listing is not None and str(listing.title or "").strip():
            return str(listing.title).strip()[:80]
        return f"Deal #{deal_id}"
    if case.deal_app == "services":
        from apps.services.services import get_visible_service

        service = get_visible_service(deal_id, None)
        if service is not None and str(service.title or "").strip():
            return str(service.title).strip()[:80]
        return f"Deal #{deal_id}"
    return f"Deal #{deal_id}"


# ------------------------------------------------------------ case writes --


@transaction.atomic
def open_case(
    *,
    opener,
    deal_app: str,
    deal_id,
    what_promised: str,
    what_happened: str,
    now=None,
) -> DisputeCase:
    """A party opens THE case on their deal (atomic: case + auto-linked
    evidence, or nothing).

    Party/visibility gate per origin (see _deal_readable); barter requires
    SENT terms (a draft negotiation is not yet a deal to dispute) and
    normalizes deal_id to the lineage ROOT — the per-deal unique and the
    reviews gate then key on one id. A second OPEN case on the same deal
    is refused; after the first is decided, re-opening is legal.
    """
    _require_auth(opener)
    if deal_app not in ALLOWED_DEAL_APPS:
        raise ValidationError(MSG_BAD_APP)
    try:
        deal_id = int(deal_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError(MSG_BAD_DEAL) from exc

    if deal_app == "barter":
        row = _barter_root(deal_id, opener)
        if row is None:
            raise ValidationError(MSG_BAD_DEAL)
        if not row.is_sent:
            raise ValidationError(MSG_DRAFT_ONLY)
        root = row.walk_to_root() if row.parent_id else row
        deal_id = root.pk
    elif not _deal_readable(deal_app, deal_id, opener):
        raise ValidationError(MSG_BAD_DEAL)

    if DisputeCase.objects.filter(
        deal_app=deal_app, deal_id=deal_id, status=OPEN
    ).exists():
        raise ValidationError(MSG_ALREADY_OPEN)

    case = DisputeCase(
        opener=opener,
        deal_app=deal_app,
        deal_id=deal_id,
        what_promised=(what_promised or "").strip(),
        what_happened=(what_happened or "").strip(),
        status=OPEN,
        opened_at=_now(now),
    )
    errors: dict[str, str] = {}
    if not case.what_promised:
        errors["what_promised"] = _("Write what was promised — the deal as agreed.")
    if not case.what_happened:
        errors["what_happened"] = _("Write what happened, for your side.")
    if errors:
        raise ValidationError(errors)
    case.full_clean()  # allow-list + bounds, in words
    case.save()
    _autolink_evidence(case, opener)
    return case


def _autolink_evidence(case: DisputeCase, opener) -> None:
    """Auto-link evidence BY ID at open time (spec: "messages auto-linked
    from the negotiation thread").

    barter: each SENT offer version of the lineage becomes a MESSAGE-kind
    row (the offer lineage IS the negotiation trail there). listings/
    services: the newest listing-enquiry thread in which the OPENER
    participates contributes its newest-window messages (ids only). No
    content copied; the detail view resolves rows lazily.
    """
    if case.deal_app != "barter":
        _autolink_chat_messages(case, opener)
        return
    from apps.barter import services as barter_services

    row = _barter_root(case.deal_id, opener) or _barter_root(case.deal_id)
    if row is None:
        return
    for version in barter_services.lineage_trail(row):
        if not version.is_sent:
            continue  # unsent drafts are private workspaces, not evidence
        _attach_row(
            EvidenceAttachment(
                case=case,
                added_by=opener,
                kind=EvidenceKind.MESSAGE.value,
                offer_id=version.pk,
            )
        )


def _autolink_chat_messages(case: DisputeCase, opener) -> None:
    """The messaging side of auto-linking (listings/services deals)."""
    from apps.messaging import services as messaging_services
    from apps.messaging.models import Thread, ThreadOrigin

    thread = (
        Thread.objects.for_participant(opener)
        .filter(
            origin_kind=ThreadOrigin.LISTING.value,
            origin_listing_id=case.deal_id,
        )
        .order_by("-created_at")
        .first()
    )
    if thread is None:
        return
    for message in messaging_services.messages_window(
        thread, EVIDENCE_MESSAGE_WINDOW
    ):
        _attach_row(
            EvidenceAttachment(
                case=case,
                added_by=opener,
                kind=EvidenceKind.MESSAGE.value,
                thread_id=thread.pk,
                message_id=message.pk,
            )
        )


def _attach_row(row: EvidenceAttachment) -> EvidenceAttachment | None:
    """Save-or-dedupe: auto-links can collide with the per-case unique
    anchors; a duplicate link is simply skipped (idempotent)."""
    try:
        row.full_clean()
        row.save()
    except IntegrityError:
        return None
    return row


@transaction.atomic
def attach_text_evidence(*, case, added_by, text: str):
    """A short human note as evidence (a party, or a moderator gathering)."""
    _require_auth(added_by)
    _assert_case_worker(case, added_by)
    if not (text or "").strip():
        raise ValidationError(MSG_EMPTY_TEXT)
    row = EvidenceAttachment(
        case=case, added_by=added_by, kind=EvidenceKind.TEXT.value, text=text
    )
    row.full_clean()
    row.save()
    return row


@transaction.atomic
def attach_image_evidence(*, case, added_by, image):
    """Attach an EXISTING MediaRef (Prompt 06 output) BY ID.

    KDPA: a member attaches their OWN upload; a moderator may gather any
    (the human gate needs the evidence to decide). Never raw files —
    uploads happen in the media app's pipeline; nothing image-shaped is
    copied into this app.
    """
    _require_auth(added_by)
    _assert_case_worker(case, added_by)
    from apps.media.models import MediaRef

    if not isinstance(image, MediaRef):
        image = _media_ref(image)
    if image is None:
        raise ValidationError(MSG_EMPTY_IMAGE)
    from apps.moderation.services import is_moderator

    if image.owner_id != added_by.pk and not is_moderator(added_by):
        raise ValidationError(MSG_NOT_YOUR_IMAGE)
    row = EvidenceAttachment(
        case=case,
        added_by=added_by,
        kind=EvidenceKind.IMAGE.value,
        image_ref_id=image.pk,
    )
    # The per-case unique anchor makes a double attachment an honest
    # refusal, not a crash (the auto-links dedupe the same way).
    saved = _attach_row(row)
    if saved is None:
        raise ValidationError(MSG_ALREADY_EVIDENCE)
    return saved


def _media_ref(image_id):
    """Resolve a raw id (UUID/str) to a MediaRef or None — honest refusal
    for bad shapes, never a fabricated reference."""
    try:
        from apps.media.models import MediaRef

        return MediaRef.objects.get(pk=image_id)
    except Exception:  # noqa: BLE001 — absent/garbled ids are handled state
        return None


def _assert_case_worker(case, user) -> None:
    """A party of the case may attach evidence — the counterpart included
    (both sides prove their story); moderators always can."""
    _require_auth(user)
    if user.pk == case.opener_id:
        return
    from apps.moderation.services import is_moderator

    if is_moderator(user):
        return
    if user.pk in deal_parties_ids(case.deal_app, case.deal_id, for_user=user):
        return
    raise ValidationError(MSG_NOT_A_PARTY)


@transaction.atomic
def start_review(case, *, moderator, now=None):
    """OPEN -> UNDER_REVIEW: a moderator picked the case up. One audit row
    rides moderation's REVIEW_STARTED vocabulary, targeting the deal object
    when reachable and the case row otherwise (moderation's own settled-
    vanished-target precedent)."""
    from apps.moderation.services import is_moderator

    if not is_moderator(moderator):
        raise ValidationError(MSG_ONLY_MODERATORS)
    if case.status != OPEN:
        raise ValidationError(_("That case is not waiting to be picked up."))
    case.status = UNDER_REVIEW
    case.save(update_fields=["status", "updated_at"])
    target = case_deal_object(case, moderator) or case
    _audit_row(action="REVIEW_STARTED", actor=moderator, target=target, reason="")
    return case


@transaction.atomic
def propose_outcome(
    case, *, proposer, action: str, note: str = "", now=None
) -> DisputeProposal:
    """A moderator PROPOSES one outcome (gate step 1 — executes NOTHING).

    The proposal's target is computed from the deal: warn/suspend land on
    the accused member (the deal party that is NOT the opener); remove-
    listing/mark-review land on the status-bearing sibling object (the
    listing/service — for barter, its origin listing reference).
    Unresolvable combinations refuse honestly — never a guessed target.
    Proposing on an OPEN case also picks it up (UNDER_REVIEW).
    """
    from apps.moderation.services import is_moderator

    if not is_moderator(proposer):
        raise ValidationError(MSG_ONLY_MODERATORS)
    case.refresh_from_db()  # status could have settled while queued
    if not case.is_open_flow:
        raise ValidationError(MSG_CASE_CLOSED)
    try:
        action_value = ProposalAction(action).value
    except ValueError as exc:
        raise ValidationError(_("That outcome does not exist.")) from exc

    parties = deal_parties_ids(case.deal_app, case.deal_id, for_user=proposer)
    accused = sorted(parties - {case.opener_id})
    if action_value in (ProposalAction.WARN.value, ProposalAction.SUSPEND.value):
        if not accused:
            raise ValidationError(MSG_NO_COUNTERPARTY)
        from django.contrib.auth import get_user_model

        target = get_user_model().objects.get(pk=accused[0])
    else:
        target = None
        if case.deal_app == "barter":
            # The removal target for a barter deal is its ORIGIN LISTING
            # (root.origin_listing_id) — the Offer row itself is negotiation
            # state, not marketplace content.
            from apps.barter.models import OfferOrigin

            root = case_deal_object(case, proposer)
            if (
                root is not None
                and root.origin_kind == OfferOrigin.LISTING.value
                and root.origin_listing_id
            ):
                from apps.listings.services import get_visible_listing

                target = get_visible_listing(root.origin_listing_id, proposer)
        else:
            target = case_deal_object(case, proposer)
        if target is None:
            raise ValidationError(MSG_NO_LINKED_LISTING)

    if case.status == OPEN:
        case.status = UNDER_REVIEW
        case.save(update_fields=["status", "updated_at"])
    proposal = DisputeProposal(
        case=case,
        proposed_action=action_value,
        target_ct=_ct_of(target),
        target_id=target.pk,
        proposed_by=proposer,
        note=(note or "").strip(),
    )
    proposal.full_clean()
    proposal.save()
    return proposal


@transaction.atomic
def confirm_proposal(proposal, *, confirmer, decision_note: str = "", now=None):
    """Gate step 2 — a DIFFERENT human confirms; the outcome executes NOW,
    through moderation's public machinery, and the case resolves.

    Same-human, already-confirmed, non-moderator and closed-case
    confirmations refuse BEFORE anything executes; everything runs in one
    transaction, so a mid-execution refusal rolls the whole gate back.
    """
    from apps.moderation.services import (
        apply_removal,
        confirm_suspension,
        draft_suspension,
        is_moderator,
    )

    if not is_moderator(confirmer):
        raise ValidationError(MSG_ONLY_MODERATORS)
    if proposal.is_confirmed:
        raise ValidationError(MSG_ALREADY_CONFIRMED)
    if confirmer.pk == proposal.proposed_by_id:
        raise ValidationError(MSG_TWO_ACTORS)
    case = proposal.case
    case.refresh_from_db()
    if not case.is_open_flow:
        raise ValidationError(MSG_CASE_CLOSED)

    target = proposal.target
    action = proposal.proposed_action
    if action == ProposalAction.SUSPEND.value:
        # Two-actor suspension, REUSING moderation's gate end-to-end: the
        # proposal's author drafts (its Step 1), the confirmer confirms
        # (its Step 2 — which independently refuses drafter==confirmer and
        # deactivates the account). Audit rows carry both actors.
        draft = draft_suspension(
            moderator=proposal.proposed_by,
            target_user=target,
            reason=(
                proposal.note
                or f"Dispute case #{case.pk} on {case.deal_app}#{case.deal_id}"
            ),
            now=now,
        )
        confirm_suspension(draft, confirmer=confirmer, now=now)
    elif action == ProposalAction.WARN.value:
        _audit_row(action=AUDIT_WARN, actor=confirmer, target=target, reason="")
    elif action == ProposalAction.REMOVE_LISTING.value:
        if not apply_removal(target):
            raise ValidationError(
                _("That target cannot be removed (no status lifecycle).")
            )
        _audit_row(
            action="REMOVED",
            actor=confirmer,
            target=target,
            reason="",  # the case row itself carries the claim
        )
    elif action == ProposalAction.MARK_REVIEW.value:
        # listings' public review_flagged field is the in-product "marked
        # for review" surface; ServiceListings expose status only — the
        # audit row is the mark there (no parallel field invented).
        if hasattr(target, "review_flagged"):
            target.review_flagged = True
            target.save(update_fields=["review_flagged", "updated_at"])
        _audit_row(
            action=AUDIT_MARK_REVIEW, actor=confirmer, target=target, reason=""
        )

    now = _now(now)
    proposal.confirmed_by = confirmer
    proposal.confirmed_at = now
    proposal.save(update_fields=["confirmed_by", "confirmed_at", "updated_at"])
    case.status = RESOLVED
    case.outcome = action
    case.decided_by = confirmer
    case.decided_at = now
    case.decided_note = (decision_note or "").strip()[:MAX_DECISION_NOTE_CHARS]
    case.save(
        update_fields=[
            "status", "outcome", "decided_by", "decided_at", "decided_note",
            "updated_at",
        ]
    )
    return proposal


@transaction.atomic
def resolve_case(case, *, moderator, decision_note: str = "", now=None):
    """A moderator closes the case as handled (e.g. the parties settled it
    themselves) WITHOUT executing a proposed outcome. Aging never calls
    this — humans only, spec verbatim: never silently close."""
    from apps.moderation.services import is_moderator

    if not is_moderator(moderator):
        raise ValidationError(MSG_ONLY_MODERATORS)
    case.refresh_from_db()
    if not case.is_open_flow:
        raise ValidationError(MSG_CASE_CLOSED)
    now = _now(now)
    case.status = RESOLVED
    case.decided_by = moderator
    case.decided_at = now
    case.decided_note = (decision_note or "").strip()[:MAX_DECISION_NOTE_CHARS]
    case.save(
        update_fields=[
            "status", "decided_by", "decided_at", "decided_note", "updated_at"
        ]
    )
    return case


@transaction.atomic
def dismiss_case(case, *, moderator, decision_note: str = "", now=None):
    """A moderator dismisses the claim (nothing wrong found). One DISMISSED
    audit row rides moderation's own vocabulary, targeting the deal object
    when reachable — the case row is the durable record either way."""
    from apps.moderation.services import is_moderator

    if not is_moderator(moderator):
        raise ValidationError(MSG_ONLY_MODERATORS)
    case.refresh_from_db()
    if not case.is_open_flow:
        raise ValidationError(MSG_CASE_CLOSED)
    target = case_deal_object(case, moderator)
    if target is not None:
        _audit_row(action="DISMISSED", actor=moderator, target=target, reason="")
    now = _now(now)
    case.status = DISMISSED
    case.decided_by = moderator
    case.decided_at = now
    case.decided_note = (decision_note or "").strip()[:MAX_DECISION_NOTE_CHARS]
    case.save(
        update_fields=[
            "status", "decided_by", "decided_at", "decided_note", "updated_at"
        ]
    )
    return case


# ------------------------------------------------------------- helpers -----


def _ct_of(target):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(target)


def _audit_row(*, action, actor, target, reason=""):
    """One immutable ModeratorAudit row via moderation's PUBLIC model — the
    sanctioned creation path (row creation allowed; mutation and deletion
    refused at ORM level)."""
    from apps.moderation.models import ModeratorAudit

    return ModeratorAudit.objects.create(
        actor=actor,
        action=action,
        target_ct=_ct_of(target),
        target_id=target.pk,
        reason=reason or "",
    )


def case_deal_object(case: DisputeCase, viewer=None):
    """The deal OBJECT behind a case (generic, lazily resolved) — used for
    audit targets and detail headers. None when the origin surface no
    longer resolves: rows are never fabricated; the case keeps the ids so
    evidence stays intact regardless."""
    if case.deal_app == "barter":
        return _barter_root(case.deal_id)
    if case.deal_app == "listings":
        from apps.listings.services import get_visible_listing

        return get_visible_listing(case.deal_id, viewer)
    if case.deal_app == "services":
        from apps.services.services import get_visible_service

        return get_visible_service(case.deal_id, viewer)
    return None


def cases_for(user):
    """The member's dispute surface: cases I opened, plus open/under-review
    cases against me (deal parties resolved per origin, viewer-gated so a
    REMOVED listing's own seller still sees the case)."""
    _require_auth(user)
    seen: dict[int, DisputeCase] = {}
    for case in DisputeCase.objects.filter(opener=user).order_by("-opened_at"):
        seen[case.pk] = case
    candidates = (
        DisputeCase.objects.filter(status__in=OPEN_FLOW)
        .exclude(opener=user)
        .order_by("-opened_at")[:100]
    )
    for case in candidates:
        if case.pk in seen:
            continue
        ids = deal_parties_ids(case.deal_app, case.deal_id, for_user=user)
        if user.pk in ids:
            seen[case.pk] = case
    return sorted(seen.values(), key=lambda c: c.opened_at, reverse=True)


def evidence_rows_for(case: DisputeCase, viewer) -> list[dict]:
    """Render-ready evidence rows: each resolved LAZILY through its origin
    door; a viewer who cannot see the underlying object gets an honest
    "not visible to you" line instead of the content (IDOR shape: same
    answer as missing — messaging/chat threads are participation-gated).

    WHY moderators may read chat rows cross-app: moderation's own queue
    resolves reported Message rows generically (its Report.target) — the
    human gate NEEDS evidence to decide; that is the sanctioned precedent,
    and the display shows only what the thread itself holds.
    """
    from apps.moderation.services import is_moderator

    viewer_pk = getattr(viewer, "pk", None)
    viewer_mod = is_moderator(viewer)
    rows = []
    for row in case.evidence.all():
        entry = {
            "row": row,
            "kind": row.kind,
            "added_by": row.added_by,
            "created_at": row.created_at,
            "visible": False,
            "payload": None,
        }
        if row.kind == EvidenceKind.MESSAGE.value:
            if row.message_id:
                visible, payload = _resolve_message(row, viewer, viewer_mod)
            else:
                visible, payload = _resolve_offer_version(row, viewer, viewer_mod)
            entry["visible"], entry["payload"] = visible, payload
        elif row.kind == EvidenceKind.IMAGE.value:
            ref = _media_ref(row.image_ref_id)
            own = bool(viewer_pk) and ref is not None and ref.owner_id == viewer_pk
            entry["visible"] = ref is not None and (own or viewer_mod)
            entry["payload"] = {
                "ref": ref,
                "detail": (
                    f"{ref.standard_bytes // 1024} KB · "
                    f"{ref.standard_w}×{ref.standard_h}"
                    if ref is not None
                    else _("the image was removed")
                ),
            }
        else:  # TEXT — plain human-typed note
            entry["visible"] = True
            entry["payload"] = {"text": row.text}
        rows.append(entry)
    return rows


def _resolve_message(row, viewer, viewer_mod):
    from apps.messaging.models import Message
    from apps.messaging import services as messaging_services

    thread = messaging_services.get_thread_for(viewer, row.thread_id)
    if viewer_mod and thread is None:
        # Human-gate precedent: moderators resolve the row itself.
        try:
            thread = Message.objects.get(pk=row.message_id).thread
        except Message.DoesNotExist:
            thread = None
    if thread is None:
        return False, None
    try:
        message = thread.messages.get(pk=row.message_id)
    except Message.DoesNotExist:
        return True, {"gone": True}  # thread visible, the message vanished
    return True, {
        "sender": message.sender.get_username(),
        "body": message.body,
        "when": message.created_at,
    }


def _resolve_offer_version(row, viewer, viewer_mod):
    from apps.barter.models import Offer
    from apps.barter import services as barter_services

    offer = barter_services.get_offer_for(viewer, row.offer_id)
    if offer is None and viewer_mod:
        # Moderator human-gate read (moderation's queue precedent).
        try:
            offer = Offer.objects.select_related("initiator", "counterpart").get(
                pk=row.offer_id
            )
        except Offer.DoesNotExist:
            offer = None
    if offer is None:
        return False, None
    items = ", ".join(str(i.item) for i in offer.items.all()[:5]) or "—"
    return True, {
        "version": offer.pk,
        "mode": offer.mode,
        "cash": offer.cash_amount,
        "status": offer.status,
        "items": items,
        "when": offer.created_at,
    }


# ------------------------------------------------------- aging & queue -----


def case_age(case: DisputeCase, now=None) -> timedelta:
    """How long the case has been open (injected clock; falls back to the
    row's created_at when opened_at is somehow unset)."""
    stamp = case.opened_at or case.created_at
    return _now(now) - stamp


def aging_tier(case: DisputeCase, now=None) -> str:
    """0–3d normal · 3–7d attention · ≥7d escalated (Prompt 23 spec).

    WHY read-time (no worker, no sweep): a missed cron can never 'silently
    close' anything — the tier is computed whenever the dashboard reads the
    case, and NOTE: this function writes nothing.
    """
    hours = case_age(case, now).total_seconds() / 3600.0
    if hours < AGE_NORMAL_DAYS * 24.0:
        return CaseTier.NORMAL.value
    if hours < AGE_ESCALATED_DAYS * 24.0:
        return CaseTier.ATTENTION.value
    return CaseTier.ESCALATED.value


def queue_cases():
    """The dashboard queue: open + under-review cases, oldest first."""
    return DisputeCase.objects.filter(status__in=OPEN_FLOW).order_by("opened_at")


def escalated_cases(now=None):
    """Just the ESCALATED tier subset — the dashboard's urgent shelf."""
    return [
        case
        for case in queue_cases()
        if aging_tier(case, now) == CaseTier.ESCALATED.value
    ]


# --------------------------------------------------------- fraud flags -----


# Off-platform payment phrases (Prompt 23 verbatim examples included; sw
# phrasing too). Matched lowercase-contains on normalized text. The flag
# note quotes the PHRASE, never the member's phone digits (KDPA: numbers
# are not copied into new rows).
OFF_PLATFORM_PHRASES = (
    "send m-pesa",
    "tuma pesa",
    "kulipa nje ya app",
    "pay outside",
    "pay directly",
)

BULK_WINDOW_DAYS = 1        # the "created in 24h" window
NEW_ACCOUNT_DAYS = 7        # account age below this counts as new
BULK_LISTING_THRESHOLD = 5  # MORE than this many listings in the window


def flag_scan(*, now=None) -> int:
    """Run all three heuristics; return how many NEW FraudFlag rows were
    written.

    PURE ANNOTATION (the Prompt 23 pitfall, enforced by omission — this
    function writes FraudFlag rows and NOTHING else): no listing status,
    no review_flag, no user.is_active, no suspension drafts, no audit rows.
    `now` drives the account-age and bulk windows (tests stay deterministic;
    under an injected PAST clock, accounts joined later simply don't
    qualify — an honest, not a fabricated, scan).
    """
    now = _now(now)
    created = 0
    created += _flag_duplicate_text()
    created += _flag_new_account_bulk(now)
    created += _flag_off_platform_pay()
    return created


def _flag_listing(target, heuristic: str, note: str) -> bool:
    """Create one annotation unless that exact (target, heuristic) pair is
    already flagged — idempotent scans, no queue spam across re-runs."""
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(target)
    key = {"target_ct": ct, "target_id": target.pk, "heuristic": heuristic}
    if FraudFlag.objects.filter(**key).exists():
        return False
    FraudFlag.objects.create(
        heuristic=heuristic,
        target_ct=ct,
        target_id=target.pk,
        note=note[:300],
    )
    return True


def _normalize_text(*parts) -> str:
    """Normalization FOR duplicate-text grouping only: whitespace collapse +
    case-fold. 'Sukuma wiki fresh 2' vs 'Sukuma wiki fresh' therefore do
    NOT match (the benign-similar-produce pitfall); same text with
    different spacing/case does."""
    return " ".join(" ".join(part for part in parts if part).lower().split())


def _flag_duplicate_text() -> int:
    """Identical normalized (title+description, en+sw) across DIFFERENT
    sellers. One member restating their own listing is bookkeeping, not
    cross-account fraud — skipped."""
    from apps.core.types import ContentStatus
    from apps.listings.models import Listing

    groups: dict[str, list] = {}
    for listing in Listing.objects.filter(status=ContentStatus.PUBLISHED.value):
        key = _normalize_text(
            listing.title, listing.description,
            listing.title_sw, listing.description_sw,
        )
        if key:
            groups.setdefault(key, []).append(listing)
    created = 0
    for group in groups.values():
        sellers = {row.seller_id for row in group}
        if len(group) < 2 or len(sellers) < 2:
            continue
        note = _(
            "Identical listing text as %(n)d other listing(s) across "
            "%(accounts)d accounts."
        ) % {"n": len(group) - 1, "accounts": len(sellers)}
        for row in group:
            if _flag_listing(row, FraudHeuristic.DUPLICATE_TEXT.value, note):
                created += 1
    return created


def _flag_new_account_bulk(now) -> int:
    """Accounts younger than 7 days with MORE than 5 listings created
    within the 24h window (spec verbatim)."""
    from apps.core.types import ContentStatus
    from apps.listings.models import Listing

    young_ids = list(
        _flag_new_account_bulk_users(now).values_list("pk", flat=True)
    )
    if not young_ids:
        return 0
    bulk = (
        Listing.objects.filter(
            status=ContentStatus.PUBLISHED.value,
            created_at__gte=now - timedelta(days=BULK_WINDOW_DAYS),
            seller_id__in=young_ids,
        )
        .values("seller_id")
        .annotate(total=Count("id"))
    )
    created = 0
    from django.contrib.auth import get_user_model

    users = {
        row["seller_id"]: row["total"]
        for row in bulk
        if row["total"] > BULK_LISTING_THRESHOLD
    }
    for seller_id, total in users.items():
        user = get_user_model().objects.get(pk=seller_id)
        account_days = max(0, (now - user.date_joined).days)
        note = _(
            "%(n)d listings created in %(hours)sh; account %(days)d day(s) old."
        ) % {
            "n": total,
            "hours": BULK_WINDOW_DAYS * 24,
            "days": account_days,
        }
        if _flag_listing(user, FraudHeuristic.NEW_ACCOUNT_BULK.value, note):
            created += 1
    return created


def _flag_new_account_bulk_users(now):
    """Users joined STRICTLY within the last 7 days (as of `now`)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.filter(
        date_joined__gt=now - timedelta(days=NEW_ACCOUNT_DAYS),
        date_joined__lte=now,
    )


def _flag_off_platform_pay() -> int:
    """Published listings whose public text asks for payment off the app:
    the phrase list above OR a phone-shaped string (listings' PUBLIC
    detect_phone_patterns — the Prompt 17 detector, reused as-is)."""
    from apps.core.types import ContentStatus
    from apps.listings.models import Listing
    from apps.listings.services import detect_phone_patterns

    created = 0
    for listing in Listing.objects.filter(status=ContentStatus.PUBLISHED.value):
        public_text = " ".join(
            part
            for part in (
                listing.title,
                listing.title_sw,
                listing.description,
                listing.description_sw,
            )
            if part
        )
        lowered = _normalize_text(public_text)
        phrases = [p for p in OFF_PLATFORM_PHRASES if p in lowered]
        phones = detect_phone_patterns(public_text)
        if not phrases and not phones:
            continue
        # KDPA: the note carries COUNTS and phrase words — never the digits.
        bits = []
        if phrases:
            bits.append(
                _("payment phrase: “%(phrase)s”") % {"phrase": phrases[0]}
            )
        if phones:
            bits.append(
                _("phone-shaped text present (%(n)d)") % {"n": len(phones)}
            )
        if _flag_listing(
            listing, FraudHeuristic.OFF_PLATFORM_PAY.value, "; ".join(bits)
        ):
            created += 1
    return created


# ------------------------------------------------------- reviews gate -----


def dispute_open_for_deal(deal_app: str, deal_id) -> bool:
    """TRUE while an OPEN dispute case exists on this deal — Prompt 22's
    binding contract, exact signature: reviews.models probes
    apps.disputes.services.dispute_open_for_deal(deal_app, deal_id).

    Root-id contract: open_case() stores barter deal ids normalized to the
    lineage ROOT, and reviews' deal ids for barter come from bars' ROOT
    settlement listings (completed_deals_for returns roots) — both sides
    key on root ids by construction. Unknown apps/ids answer False (no
    gate, no crash): this function must never make a review fail loudly.
    """
    if deal_app not in ALLOWED_DEAL_APPS:
        return False
    try:
        deal_id = int(deal_id)
    except (TypeError, ValueError):
        return False
    return DisputeCase.objects.filter(
        deal_app=deal_app, deal_id=deal_id, status=OPEN
    ).exists()
