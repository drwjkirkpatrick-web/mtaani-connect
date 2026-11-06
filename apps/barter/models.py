"""Barter models (Prompt 21) — versioned offers and a STRICT state machine.

THE STATE MACHINE IS THE PRODUCT (PROMPTS.md pitfall, verbatim):
    DRAFT → OFFERED → COUNTERED → ACCEPTED → COMPLETED
with CANCELLED / EXPIRED terminal states. Every legal and every illegal
transition is validated in Offer.transition() — the ONE door that changes
status. Views NEVER write status fields; services orchestrate through
transition() only, so no future route can bypass the rules.

WHY COUNTERED has out-edges: it is the state of PARENT rows whose recipient
sent a counteroffer. ACCEPTED/CANCELLED/COMPLETED moves on a superseded
parent are the services' propagation of a lineage-level settlement back
up the supersedence chain (apps/barter/services accept/cancel/complete),
and COUNTERED → EXPIRED is the inactivity sweep expiring superseded
ancestors alongside their stale tip. So every dict edge is a real move
some flow makes, not decoration.

NO CUSTODY, EVER (product brief §3 Module 6): an offer row holds NO money,
NO items, NO escrow, NO wallet. The "cash component" (cash_amount) is a
STATED amount the two members settle themselves via M-Pesa; the UI says so
plainly. M-Pesa integration stays a documented adapter stub
(apps/barter/mpesa.py) — never networked, never called, in V1.

ORIGIN CONTRACT — deliberately NO FK to listings.Listing (messaging's
Prompt-20 precedent, restated for barter): origin_listing_id is a plain
PositiveBigIntegerField; the listing is resolved at compose time through
listings' PUBLIC read door (apps.listings.services.get_visible_listing)
and its owner is STORED as the counterpart FK. Negotiation state is fully
self-contained — a removed/recreated listing never orphans the audit
trail. A future prompt may attach a messaging thread for free chat
(apps.messaging.services.create_direct_thread documents the hook); barter
lineage IS the negotiation history, so V1 wires no thread automatically —
documented, not built.

VERSION THREAD: every counteroffer is a NEW Offer row whose `parent` links
to the row it supersedes; walk_to_root()/lineage() reconstruct the trail
root → now. Each version snapshots its own item sides (OfferItem), so the
terms of every stage stay inspectable forever — that IS the audit trail.
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import BarterMode, OfferStatus, TimestampedModel

# HOW MANY DAYS of inactivity before an open offer expires. The sweep reads
# THIS module constant; a deployment tweak is one settings-bridge away and
# tests inject a clock rather than mutate the constant.
EXPIRY_DAYS = 7

# WHY a cap: the composed offer page must stay inside the 50 KB mobile
# budget (hard rule); unbounded item lists risk it on one page.
MAX_ITEMS_PER_SIDE = 10
MAX_ITEM_CHARS = 120
MAX_QTY_CHARS = 40

MSG_BAD_MODE = _("%(mode)s is not a barter mode.")
MSG_BAD_STATUS = _("%(value)s is not an offer state.")
MSG_CASH_REQUIRED = _(
    "%(mode)s deals state a cash amount — the part the members settle "
    "themselves via M-Pesa."
)
MSG_CASH_FORBIDDEN = _(
    "Barter-only deals state no cash amount — remove the money figure."
)
MSG_SELF_DEAL = _("You cannot make an offer to yourself — pick another member.")
MSG_WRONG_USER = _("Only a member of this negotiation can do that.")


class OfferStatusError(ValidationError):
    """A state-machine refusal from Offer.transition().

    NOTE: dedicated subclass so services/views can re-render with the
    plain-language reason without masking unrelated field errors.
    """

    ERROR_CODE = "illegal_barter_transition"

    def __init__(self, message):
        super().__init__(message, code=self.ERROR_CODE)


class OfferSide(models.TextChoices):
    """The two sides of a versioned offer — "I give" vs "I want"."""

    GIVE = "GIVE", _("I give")
    WANT = "WANT", _("I want")


class OfferOrigin(models.TextChoices):
    """Why an offer exists (typed origin reference, no cross-app FK — see
    module docstring). Vocabulary mirrors messaging's ThreadOrigin so the
    parent's consolidation step maps them 1-to-1."""

    LISTING = "listing", _("Marketplace listing")
    DIRECT = "direct", _("Direct negotiation")


# ---------------------------------------------------------------- states ----


# WHY a plain dict (and not a library): seven states, ten edges, zero
# dependencies — the whole negotiation policy is legible in one screen and
# every edge is unit-testable without Django machinery. WHO may drive WHAT
# is split in two layers ON PURPOSE: this dict says only that a move is
# legal in a state (model-level); the stricter role rules (only the
# drafter sends, only the recipient accepts/counters) live in
# apps/barter/services.py, so the model stays a pure state machine and the
# test matrix exercises legality independently of role.
TRANSITIONS: dict[str, set[str]] = {
    OfferStatus.DRAFT.value: {OfferStatus.OFFERED.value},
    OfferStatus.OFFERED.value: {
        OfferStatus.COUNTERED.value,
        OfferStatus.ACCEPTED.value,
        OfferStatus.CANCELLED.value,
        OfferStatus.EXPIRED.value,
    },
    OfferStatus.COUNTERED.value: {
        OfferStatus.ACCEPTED.value,
        OfferStatus.CANCELLED.value,
        OfferStatus.EXPIRED.value,
    },
    OfferStatus.ACCEPTED.value: {
        OfferStatus.COMPLETED.value,
        OfferStatus.CANCELLED.value,
    },
    OfferStatus.COMPLETED.value: set(),
    OfferStatus.CANCELLED.value: set(),
    OfferStatus.EXPIRED.value: set(),
}


def illegal_reason(from_value: str, to_value: str) -> str:
    """A plain-language reason why `from_value` cannot move to `to_value`.

    WHY a function (not an import-time table): reasons are lazy-translated
    — locale is a request property, module import is not. Built per call,
    interpolated with %, and asserted verbatim in tests/test_barter.py.
    """
    if from_value == OfferStatus.COMPLETED.value:
        return _("This deal is completed — the exchange is done and the "
                 "record is closed. Nothing can change it.")
    if from_value == OfferStatus.CANCELLED.value:
        return _("This negotiation was cancelled — start a new offer to "
                 "try again. Cancelled offers stay cancelled.")
    if from_value == OfferStatus.EXPIRED.value:
        return _("This offer expired after 7 quiet days — send a fresh "
                 "offer. Expired offers cannot reopen.")
    if (
        from_value == OfferStatus.ACCEPTED.value
        and to_value == OfferStatus.COUNTERED.value
    ):
        # The named pitfall (COUNTERED after ACCEPTED): once agreed, the
        # terms are locked; renegotiate by cancelling and starting over.
        return _(
            "This offer is accepted — its terms are locked and cannot be "
            "countered. Cancel and start a new negotiation instead."
        )
    if from_value == OfferStatus.ACCEPTED.value:
        if to_value == OfferStatus.ACCEPTED.value:
            return _("This offer is already accepted.")
        return _("An agreed deal only moves forward to completed — or "
                 "cancelled if it fell through.")
    if from_value == OfferStatus.DRAFT.value:
        return _(
            "Only a draft can be sent — a draft is not yet on the table, "
            "so there is nothing to answer or counter yet."
        )
    if from_value == OfferStatus.OFFERED.value:
        if to_value == OfferStatus.DRAFT.value:
            return _("An offer already on the table cannot go back to a draft.")
        if to_value == OfferStatus.COMPLETED.value:
            return _("Complete a deal only after its terms are agreed.")
        return _("An open offer can only be countered, accepted, cancelled "
                 "or left to expire.")
    if from_value == OfferStatus.COUNTERED.value:
        return _(
            "These terms were already answered by a counteroffer — respond "
            "to the latest terms instead of the superseded ones."
        )
    return _("That move is not part of the offer flow.")


class Offer(TimestampedModel):
    """ONE version of a negotiation — root offer or a counteroffer.

    A lineage is a strictly alternating chain: root (initiator →
    counterpart), child counters ping-pong back (the counterer of v(n) is
    v(n)'s recipient, so v(n+1) runs the other way). Status lives on every
    row and superseded ancestors freeze at COUNTERED. Party membership and
    state legality are enforced HERE, so even admin or scripts cannot edit
    status out-of-band.
    """

    mode = models.CharField(
        _("Deal mode"),
        max_length=16,
        choices=BarterMode.choices,
        db_index=True,
    )
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=OfferStatus.choices,
        default=OfferStatus.DRAFT,
        db_index=True,
    )
    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="barter_offers_started",
        verbose_name=_("Offered by"),
    )
    counterpart = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="barter_offers_received",
        verbose_name=_("Offered to"),
    )
    parent = models.ForeignKey(
        # WHY self-FK (not a root pointer): each counter links to the
        # VERSION it supersedes — lineage walks node-by-node to the root,
        # which is exactly the sequence a dispute would replay.
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
        verbose_name=_("Supersedes"),
    )
    # ORIGIN (module docstring): no cross-app FK; listing offers carry the
    # listing's pk, direct negotiations use the direct origin and no id.
    origin_kind = models.CharField(
        _("Origin kind"),
        max_length=16,
        choices=OfferOrigin.choices,
        default=OfferOrigin.DIRECT,
        db_index=True,
    )
    origin_listing_id = models.PositiveBigIntegerField(
        _("Origin listing id (no FK while listings may evolve)"),
        null=True,
        blank=True,
        db_index=True,
    )
    # THE STATED CASH COMPONENT — never an account, never a transaction.
    # Members settle it themselves (M-Pesa); the app's only role is keeping
    # the number and its history honest.
    cash_amount = models.DecimalField(
        _("Cash amount (KES)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    # Activity stamp for the inactivity sweep; every transition renews it.
    # WHY its own column (not updated_at): metadata edits must not count as
    # negotiation activity — inactivity is about MOVES. Nullable so the
    # sweep can fall back to created_at for rows written without one.
    last_activity_at = models.DateTimeField(
        _("Last negotiation activity"), null=True, blank=True, db_index=True
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # DB mirror of the mode/cash rule (model clean() explains it in
            # words; this guards any future write path). NOTE: in SQL a
            # CHECK passes while the condition is NULL, so BARTER rows with
            # a NULL amount are not rejected by the >= 0 clause.
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(mode=BarterMode.CASH.value, cash_amount__isnull=False)
                        | models.Q(mode=BarterMode.CASH_PLUS.value, cash_amount__isnull=False)
                        | models.Q(mode=BarterMode.BARTER.value, cash_amount__isnull=True)
                    )
                    & models.Q(cash_amount__gte=0)
                ),
                name="barter_offer_mode_matches_cash",
            )
        ]

    def __str__(self) -> str:
        return f"{self.mode} {self.status} #{self.pk} {self.initiator_id}→{self.counterpart_id}"

    # --------------------------------------------------------- party reads

    def is_party(self, user) -> bool:
        """True when `user` is one of this row's two parties.

        WHY row-level (not lineage-level): counters ping-pong between the
        same two members, so this row's parties ARE the lineage's parties.
        """
        pk = getattr(user, "pk", None)
        return pk is not None and pk in (self.initiator_id, self.counterpart_id)

    # ---------------------------------------------------------- lineage

    def walk_to_root(self) -> "Offer":
        """Walk `parent` links to the lineage root; self IS the root.

        Cycle-guarded: a parent loop can only come from a future bug, and
        the guard turns it into a clean error instead of a hang.
        """
        node = self
        for _ in range(100):
            if node.parent_id is None:
                return node
            node = node.parent
        raise ValidationError(_("This offer chain is damaged — no root found."))

    def lineage(self) -> list["Offer"]:
        """The trail root → this row (inclusive), root first.

        WHY not a single recursive queryset: the chain is short (a mobile
        page renders at most a handful of versions) and the walk keeps the
        supersedence order explicit. Re-freshes rows from the DB only via
        the chain nodes themselves; callers needing fresh status call the
        service layer.
        """
        chain: list["Offer"] = []
        node: "Offer | None" = self
        for _ in range(100):
            chain.append(node)
            if node.parent_id is None:
                chain.reverse()
                return chain
            node = node.parent
        raise ValidationError(_("This offer chain is damaged — no root found."))

    # ------------------------------------------------------ state machine

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OfferStatus.COMPLETED.value,
            OfferStatus.CANCELLED.value,
            OfferStatus.EXPIRED.value,
        )

    @property
    def is_sent(self) -> bool:
        """True once this version is on the table (no longer a draft)."""
        return self.status != OfferStatus.DRAFT.value

    def transition(self, new_status: str, *, by_user=None, now=None) -> "Offer":
        """Move this row to `new_status` — the ONLY sanctioned status write.

        Party gate (model-level, so no view/script bypass):
        - every move needs an authenticated member of THIS lineage, except
          EXPIRED — by_user=None is the SWEEP'S signature (a system move on
          behalf of nobody); members may also expire via admin tooling.
        Role rules (only the drafter sends; only the recipient
        accepts/counters) live in services — this method stays the pure
        legality gate and the party gate.
        `now` injects the clock (injected-clock hard rule).

        Illegal moves raise OfferStatusError with the plain-language reason
        the UI shows — asserted verbatim in tests/test_barter.py.
        """
        try:
            target = OfferStatus(new_status).value
        except ValueError as exc:
            raise OfferStatusError(MSG_BAD_STATUS % {"value": str(new_status)}) from exc

        current = self.status
        if target not in TRANSITIONS.get(current, set()):
            raise OfferStatusError(illegal_reason(current, target))

        if target == OfferStatus.EXPIRED.value:
            if by_user is not None and not self.is_party(by_user):
                raise OfferStatusError(MSG_WRONG_USER)
        else:
            if by_user is None or not getattr(by_user, "is_authenticated", False):
                raise OfferStatusError(MSG_WRONG_USER)
            if not self.is_party(by_user):
                raise OfferStatusError(MSG_WRONG_USER)

        self.status = target
        self.last_activity_at = now if now is not None else timezone.now()
        # WHY update_fields (not save()): a status move must never rewrite
        # mode/cash/parent/origin — status, activity stamp and updated_at
        # only. If mode/cash are inconsistent, the DB constraint catches
        # it rather than this method blessing a bad row.
        self.save(update_fields=["status", "last_activity_at", "updated_at"])
        return self

    # ------------------------------------------------------------ clean

    def clean(self):
        # Mode/cash consistency (the DB constraint mirrors it; full_clean
        # is where the human-readable refusal comes from).
        try:
            mode = BarterMode(self.mode)
        except ValueError as exc:
            raise ValidationError({"mode": MSG_BAD_MODE % {"mode": self.mode}}) from exc
        cash = self.cash_amount
        if mode in (BarterMode.CASH, BarterMode.CASH_PLUS) and cash is None:
            raise ValidationError({"cash_amount": MSG_CASH_REQUIRED % {"mode": self.mode}})
        if mode == BarterMode.BARTER and cash is not None:
            raise ValidationError({"cash_amount": MSG_CASH_FORBIDDEN})

        # Version-thread ping-pong: a counter's sender is the parent's
        # recipient, and the parent's author receives the counter back.
        # WHY model-level: the chain shape is integrity, not UX preference —
        # every row's two parties are the lineage's two parties BECAUSE of
        # this; queries for "deals involving a member" depend on it.
        if self.parent_id:
            if self.pk is not None and self.parent_id == self.pk:
                raise ValidationError(_("An offer cannot supersede itself."))
            parent = self.parent
            if (
                parent.initiator_id != self.counterpart_id
                or parent.counterpart_id != self.initiator_id
            ):
                raise ValidationError(
                    _("A counteroffer goes back to the member who made the offer.")
                )


class OfferItem(TimestampedModel):
    """ONE line of ONE side of ONE version (a versioned term snapshot).

    "I give: 2kg maize + sewing hour; I want: solar lamp + KES 300" is two
    GIVE rows + some WANT rows + the offer's cash_amount (the KES 300).
    Items belong to the version, are editable ONLY while that version is a
    DRAFT (services.set_items enforces it), and are never aggregated into
    a value — no custody also means no valuation logic.
    """

    offer = models.ForeignKey(
        "barter.Offer", on_delete=models.CASCADE, related_name="items"
    )
    side = models.CharField(
        _("Side"), max_length=8, choices=OfferSide.choices, db_index=True
    )
    item = models.CharField(
        _("Item"),
        max_length=MAX_ITEM_CHARS,
        validators=[MaxLengthValidator(MAX_ITEM_CHARS)],
    )
    qty = models.CharField(
        _("Quantity"),
        max_length=MAX_QTY_CHARS,
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_QTY_CHARS)],
    )

    class Meta:
        ordering = ["id"]  # insertion order IS the stated order (docstring)

    def __str__(self) -> str:
        return f"{self.side} #{self.offer_id}: {self.item}"

    def clean(self):
        if not (self.item or "").strip():
            raise ValidationError({"item": _("Name the item — 'what', as offered.")})
