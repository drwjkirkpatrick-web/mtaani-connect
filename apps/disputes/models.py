"""Disputes models (Prompt 23) — case, evidence, proposals, fraud flags.

Design decisions for the next cold-start reader:
- A case references a deal EXTERNALLY (deal_app + deal_id, the generic
  app-label contract Prompt 22 established): barter (root Offer pk),
  listings (Listing pk), services (ServiceListing pk). deal_id is ALWAYS
  stored normalized to the barter ROOT row so all queries (the per-deal
  open-case unique, the reviews 1-star gate door) key on one id.
- ONE open case per deal, DB-enforced by a partial UniqueConstraint — the
  re-open after it is decided is legal (an old fight, newly joined).
- Evidence rows store IDS ONLY (KDPA minimization): messaging Message ids,
  barter Offer version ids, MediaRef UUIDs. No message body, no image
  bytes, no user contact data is ever copied into this app; the case
  detail view resolves them lazily through each sibling's public door.
- DisputeProposal is the HUMAN REVIEW GATE record: the system/moderator
  proposes, a DIFFERENT human confirms; confirming is the moment the
  outcome executes (through moderation's public machinery). Nothing
  money- or safety-adjacent fires without those two rows.
- FraudFlag is pure annotation: a heuristic's finding attached to a
  target. Flags exist to be confirmed or dismissed by humans — flag_scan
  NEVER changes any status, suspension or removal (Prompt 23 pitfall).
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel

# The generic deal-ref allow-list — Prompt 23 verbatim: "barter or sale";
# listings sales + Prompt 18 services jobs. Reviews stores the same triple.
ALLOWED_DEAL_APPS = ("barter", "listings", "services")

# Generic-FK allow-lists: proposal TARGETS are members (warn/suspend) or
# status-bearing sibling objects (remove/mark-review); fraud flags annotate
# accounts members and listings rows (the only heuristics' surfaces in V1).
PROPOSAL_TARGET_APPS = ("accounts", "listings", "services")
FLAG_TARGET_APPS = ("accounts", "listings")

MAX_CLAIM_CHARS = 2000   # structured claim: two prose fields, mobile-bounded
MAX_DECISION_NOTE_CHARS = 500
MAX_PROPOSAL_NOTE_CHARS = 500
MAX_EVIDENCE_TEXT_CHARS = 500
MAX_FLAG_NOTE_CHARS = 300


class CaseStatus(models.TextChoices):
    """Lifecycle of a dispute (in-app vocabulary, like moderation's)."""

    OPEN = "OPEN", _("Open")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    RESOLVED = "RESOLVED", _("Resolved")
    DISMISSED = "DISMISSED", _("Dismissed")


class ProposalAction(models.TextChoices):
    """Outcomes the system/moderator proposes; a human admin confirms."""

    WARN = "WARN", _("Warn the member")
    SUSPEND = "SUSPEND", _("Suspend the account")
    REMOVE_LISTING = "REMOVE_LISTING", _("Remove the listing")
    MARK_REVIEW = "MARK_REVIEW", _("Mark for review")


class EvidenceKind(models.TextChoices):
    """What one evidence row points at (ids only — see module docstring).

    NOTE: one MESSAGE-kind row anchors EITHER a messaging.Message (a chat
    reply: thread_id + message_id) OR a barter Offer version row (the
    offer lineage IS the negotiation trail there — Prompt 21 docstring).
    The distinction is the populated anchor column, never the kind label.
    """

    MESSAGE = "MESSAGE", _("Negotiation message")
    IMAGE = "IMAGE", _("Image")
    TEXT = "TEXT", _("Note")


class FraudHeuristic(models.TextChoices):
    """The three Prompt 23 heuristics. Flags annotate, never act."""

    DUPLICATE_TEXT = "DUPLICATE_TEXT", _("Duplicate listing text")
    NEW_ACCOUNT_BULK = "NEW_ACCOUNT_BULK", _("New account, bulk listings")
    OFF_PLATFORM_PAY = "OFF_PLATFORM_PAY", _("Off-platform payment phrase")


class CaseTier(models.TextChoices):
    """Aging tier for the admin dashboard (values are lowercase strings)."""

    NORMAL = "normal", _("Normal")
    ATTENTION = "attention", _("Needs attention")
    ESCALATED = "escalated", _("Escalated")


class DisputeCase(TimestampedModel):
    """One dispute about ONE deal: structured claim + evidence + decisions.

    WHY opened_at is a plain column (not auto_now_add): the injected-clock
    hard rule — aging escalation must be testable against a fixed `now`,
    and an auto stamp cannot be moved by tests.
    """

    deal_app = models.CharField(
        _("Deal origin app"), max_length=32, db_index=True
    )
    deal_id = models.PositiveBigIntegerField(_("Deal id"), db_index=True)

    opener = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="disputes_opened",
        verbose_name=_("Opener"),
    )
    what_promised = models.TextField(
        _("What was promised"),
        validators=[MaxLengthValidator(MAX_CLAIM_CHARS)],
    )
    what_happened = models.TextField(
        _("What happened"),
        validators=[MaxLengthValidator(MAX_CLAIM_CHARS)],
    )

    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=CaseStatus.choices,
        default=CaseStatus.OPEN,
        db_index=True,
    )
    opened_at = models.DateTimeField(_("Opened at"), db_index=True)

    # The human decision (proposal confirm, resolve or dismiss) — one place.
    outcome = models.CharField(
        _("Outcome"),
        max_length=16,
        choices=ProposalAction.choices,
        blank=True,
        default="",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="disputes_decided",
        verbose_name=_("Decided by"),
    )
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)
    decided_note = models.TextField(
        _("Decision note"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_DECISION_NOTE_CHARS)],
    )

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            # ONE OPEN case per deal (DB mirror of the service check —
            # admin/scripts inherit it; spec: a dispute case per deal).
            models.UniqueConstraint(
                fields=["deal_app", "deal_id"],
                condition=Q(status=CaseStatus.OPEN.value),
                name="uniq_dispute_open_per_deal",
            ),
        ]
        indexes = [
            models.Index(
                fields=["deal_app", "deal_id", "status"],
                name="dispute_deal_state_idx",
            ),
            models.Index(
                fields=["status", "opened_at"], name="dispute_queue_age_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.deal_app}#{self.deal_id} ({self.status})"

    @property
    def is_open_flow(self) -> bool:
        """True while the case can still move (open or picked up)."""
        return self.status in (
            CaseStatus.OPEN.value,
            CaseStatus.UNDER_REVIEW.value,
        )

    def clean(self):
        errors: dict = {}
        if self.deal_app not in ALLOWED_DEAL_APPS:
            errors["deal_app"] = _(
                "Disputes exist for barter, marketplace and service deals only."
            )
        for name in ("what_promised", "what_happened"):
            if not (getattr(self, name) or "").strip():
                errors[name] = _("Write what was promised and what happened.")
        if self.opened_at is None:
            errors["opened_at"] = _("A case needs its opening time.")
        # A decided case records WHO decided and WHEN — the row stays
        # (evidence retention), so its decision fields must stay honest.
        if self.status == CaseStatus.OPEN.value:
            if self.decided_by_id or self.decided_at:
                errors["status"] = _("An open case has no decision yet.")
        elif self.status in (
            CaseStatus.RESOLVED.value,
            CaseStatus.DISMISSED.value,
        ):
            if not (self.decided_by_id and self.decided_at):
                errors["status"] = _("A decided case names the decider and time.")
        if errors:
            raise ValidationError(errors)


class EvidenceAttachment(TimestampedModel):
    """One piece of evidence: a REFERENCE, never a copy (KDPA).

    Columns by kind (validated together in clean()):
    - MESSAGE + thread_id/message_id -> a messaging.Message (enquiry chat)
    - MESSAGE + offer_id             -> a barter.Offer version row
    - IMAGE  + image_ref_id          -> a media.MediaRef (UUID primary key)
    - TEXT   + text                  -> a short, human-typed note
    """

    case = models.ForeignKey(
        "disputes.DisputeCase",
        on_delete=models.CASCADE,
        related_name="evidence",
        verbose_name=_("Case"),
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dispute_evidence_added",
        verbose_name=_("Added by"),
    )
    kind = models.CharField(
        _("Kind"), max_length=12, choices=EvidenceKind.choices, db_index=True
    )
    # IDS ONLY — see module + class docstrings.
    thread_id = models.PositiveBigIntegerField(
        _("Message thread id"), null=True, blank=True
    )
    message_id = models.PositiveBigIntegerField(
        _("Message id"), null=True, blank=True
    )
    offer_id = models.PositiveBigIntegerField(
        _("Barter offer version id"), null=True, blank=True
    )
    # WHY UUIDField: media.MediaRef's primary key is a UUID (Prompt 06).
    image_ref_id = models.UUIDField(_("MediaRef id"), null=True, blank=True)
    text = models.TextField(
        _("Note text"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_EVIDENCE_TEXT_CHARS)],
    )

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["case", "message_id"],
                condition=Q(message_id__isnull=False),
                name="uniq_evidence_message_per_case",
            ),
            models.UniqueConstraint(
                fields=["case", "offer_id"],
                condition=Q(offer_id__isnull=False),
                name="uniq_evidence_offer_per_case",
            ),
            models.UniqueConstraint(
                fields=["case", "image_ref_id"],
                condition=Q(image_ref_id__isnull=False),
                name="uniq_evidence_image_per_case",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} -> {self.case_id}"

    def clean(self):
        """Exactly ONE id anchor per row: the kind determines which column
        may populate. Any mismatch refuses — an ambiguous evidence row is
        a lie to the human reviewer."""
        if self.kind == EvidenceKind.MESSAGE.value:
            if self.offer_id and self.message_id:
                raise ValidationError(
                    _("A message evidence row points at one message or one offer.")
                )
            if self.message_id and not self.thread_id:
                raise ValidationError(
                    _("A chat-message evidence row needs its thread id.")
                )
            if not (self.message_id or self.offer_id) or self.image_ref_id:
                raise ValidationError(
                    _("Message evidence needs a message id or an offer version id.")
                )
        elif self.kind == EvidenceKind.IMAGE.value:
            if not self.image_ref_id or self.thread_id or self.message_id or self.offer_id:
                raise ValidationError(_("Image evidence needs one MediaRef id."))
        elif self.kind == EvidenceKind.TEXT.value:
            if self.thread_id or self.message_id or self.offer_id or self.image_ref_id:
                raise ValidationError(
                    _("A text evidence row holds just the note — nothing else.")
                )
            if not (self.text or "").strip():
                raise ValidationError(_("Write the note the reviewer should read."))
        else:
            raise ValidationError(_("That evidence kind does not exist."))


class DisputeProposal(TimestampedModel):
    """One proposed outcome for a case — the gate record (module docstring).

    Lifecycle: created PENDING (confirmed_by/at NULL). services.
    confirm_proposal() is the only writer of the confirming columns;
    afterwards the row is immutable (save() refuses further mutations).
    """

    case = models.ForeignKey(
        "disputes.DisputeCase",
        on_delete=models.CASCADE,
        related_name="proposals",
        verbose_name=_("Case"),
    )
    proposed_action = models.CharField(
        _("Proposed action"), max_length=16, choices=ProposalAction.choices
    )
    # WHO/WHAT the outcome lands on: a user (warn/suspend) or a status-
    # bearing sibling object (remove-listing/mark-review). Generic by the
    # moderation precedent — proposals must not import sibling apps.
    target_ct = models.ForeignKey(
        ContentType,
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
        limit_choices_to={"app_label__in": PROPOSAL_TARGET_APPS},
    )
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_ct", "target_id")

    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dispute_proposals_made",
        verbose_name=_("Proposed by"),
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dispute_proposals_confirmed",
        verbose_name=_("Confirmed by"),
    )
    confirmed_at = models.DateTimeField(_("Confirmed at"), null=True, blank=True)
    note = models.TextField(
        _("Proposal note"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_PROPOSAL_NOTE_CHARS)],
    )

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(
                fields=["case", "created_at"], name="disputeprop_case_when"
            ),
        ]

    def __str__(self) -> str:
        state = "confirmed" if self.confirmed_at else "pending"
        return f"{self.proposed_action} on {self.case_id} ({state})"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None and self.confirmed_by_id is not None

    def clean(self):
        """Action/target-kind consistency + case-state check at the MODEL
        level, so every write path (service, admin, a future API) inherits
        the rules."""
        errors: dict = {}
        if self.case_id is not None:
            case = self.case
            if not case.is_open_flow:
                errors["case"] = _(
                    "Only an open (or under-review) case can receive proposals."
                )
        if self.target_ct_id is None:
            errors["target_id"] = _("A proposal names what the outcome lands on.")
        else:
            app_label = self.target_ct.app_label
            target = self.target
            if self.proposed_action in (
                ProposalAction.WARN.value,
                ProposalAction.SUSPEND.value,
            ):
                # Warn/suspend are about a MEMBER.
                if app_label != "accounts" or target is None:
                    errors["target_id"] = _("Warn and suspend apply to a member's account.")
            elif self.proposed_action in (
                ProposalAction.REMOVE_LISTING.value,
                ProposalAction.MARK_REVIEW.value,
            ):
                # Remove/mark-review land on a status-bearing sibling object.
                if target is None or not hasattr(target, "status"):
                    errors["target_id"] = _(
                        "Removal and mark-for-review apply to a listing or service."
                    )
        if errors:
            raise ValidationError(errors)


class FraudFlag(TimestampedModel):
    """A heuristic finding on ONE target — annotation only (module docstring).

    De-duplicated per (target, heuristic): a re-run of flag_scan over the
    same unchanged state adds nothing; clearing means a human deletes the
    annotation (or the state changes and the scan re-flags).
    """

    heuristic = models.CharField(
        _("Heuristic"), max_length=24, choices=FraudHeuristic.choices, db_index=True
    )
    target_ct = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="+",
        limit_choices_to={"app_label__in": FLAG_TARGET_APPS},
    )
    target_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("target_ct", "target_id")
    note = models.CharField(
        _("Why it was flagged"),
        max_length=MAX_FLAG_NOTE_CHARS,
        validators=[MaxLengthValidator(MAX_FLAG_NOTE_CHARS)],
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["target_ct", "target_id", "heuristic"],
                name="uniq_fraudflag_target_heuristic",
            )
        ]
        indexes = [
            models.Index(
                fields=["heuristic", "-created_at"], name="fraudflag_recent_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.heuristic} on {getattr(self.target, 'pk', '?')}"

    @property
    def target_summary(self) -> str:
        """Short human label for the queue page (moderation's pattern)."""
        target = self.target
        return str(getattr(target, "display_title", None) or target or "?")[:80]
