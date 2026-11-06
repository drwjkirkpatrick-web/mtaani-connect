"""Listing lifecycle (Prompt 24) — the SELLER-side state machine + pure
validation. Pure here means NO DB, NO request, NO user: everything below
takes plain values (state strings, a category, timestamps) and answers
"what is legal?" or "what applies?".

TWO AXES, ONE MODEL (why this module exists next to ContentStatus):
- ContentStatus (apps.core.types) is the MODERATION axis — is the row
  public? (DRAFT → PUBLISHED / UNDER_REVIEW / REMOVED).
- ListingLifecycle is the TRADING axis — is the seller still trading the
  item? A flagged row can still be an active sale; a sold row is history,
  not spam. The market list shows PUBLISHED *and* lifecycle-ACTIVE.

THE TABLE (single source of truth — services ask, never hand-rename):

    ACTIVE  → PAUSED (seller), SOLD (seller), EXPIRED (the 30-day sweep only)
    PAUSED  → ACTIVE (resume), SOLD (seller)
    SOLD    → terminal. Re-listing the same item = a NEW listing (a new row
              through create_listing). WHY: the original row is the audit
              trail for reviews (Prompt 22) and disputes (Prompt 23); reusing
              it would rewrite history and re-arm sold listings for buyers
              mid-negotiation.
    EXPIRED → ACTIVE strictly via the ONE-TAP RENEW service (renew_listing):
              identical content, fresh renewed_at — no re-typing. A plain
              resume() refuses EXPIRED; the only way back on the market is
              the seller's fresh timestamp.

UTILITY-FIRST ECONOMY (product brief): bump/renew are free but throttled
(EXPIRY_DAYS / BUMP_COOLDOWN_DAYS) — visibility refreshes by honest
self-service, never pay-to-shout, and never more than once per window.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus

from .models import ListingLifecycle

# ---------------------------------------------------------------------------
# Constants (mirroring apps/barter EXPIRY_DAYS pattern: module constant the
# sweep reads; deployments tune by editing the constant, tests never mutate it
# — they inject a clock instead).
EXPIRY_DAYS = 30
BUMP_COOLDOWN_DAYS = 7

MSG_ILLEGAL_MOVE = _(
    "%(from_state)s listings cannot move to %(to_state)s."
)
MSG_BUMP_TOO_SOON = _(
    "You bumped this listing recently — bump once every "
    "%(cooldown)d days, not more."
)
MSG_NOT_A_CATEGORY = _("Pick one of the listed categories.")
MSG_WRONG_SOURCE = _(
    "This tap works on %(from_value)s listings — this one is "
    "%(from_state)s."
)


class LifecycleError(ValidationError):
    """A state-machine refusal from the lifecycle rules.

    Dedicated subclass (same pattern as barter's OfferStatusError) so views
    can flash the plain-language reason without masking unrelated field
    errors.
    """

    ERROR_CODE = "illegal_lifecycle_transition"

    def __init__(self, message):
        super().__init__(message, code=self.ERROR_CODE)


# WHY frozen frozensets: the table is CONTRACT, not data — a transition that
# is legal today must not silently widen because someone mutated a set.
TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"PAUSED", "SOLD", "EXPIRED"}),
    "PAUSED": frozenset({"ACTIVE", "SOLD"}),
    "SOLD": frozenset(),  # terminal — see module docstring for re-list policy
    "EXPIRED": frozenset({"ACTIVE"}),
}


def can_transition(current: str, target: str) -> bool:
    """Is `current → target` a legal lifecycle edge? Pure. Same state is
    not in the table — sellers' no-op clicks get the plain refusal (with
    their row's actual state named), which is why even ACTIVE→ACTIVE is
    refused here rather than silently absorbed."""
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    """Raise LifecycleError with a plain-language reason when the edge is
    not in the table."""
    if not can_transition(current, target):
        raise LifecycleError(
            MSG_ILLEGAL_MOVE % {"from_state": current, "to_state": target}
        )


def category_edit_requires_reapproval(*, status: str, lifecycle: str,
                                      current_category: str,
                                      new_category: str) -> bool:
    """THE anti-bait-and-switch gate (pure predicate over plain values).

    A seller changing the CATEGORY on a live market row (PUBLISHED status,
    ACTIVE lifecycle) must earn moderator re-approval first: buyers filter
    and trust by category, so a quiet switch is advertisement fraud-shaped.
    Price/description/ward edits are deliberately NOT gated — a price tick
    is negotiation, not deception, and stays allowed while ACTIVE.

    Everything else (drafts, already-pending rows, no-op category values)
    is not the gate's business. Moderation/disputes internals are NOT ours:
    downstream, this predicate flips ContentStatus.UNDER_REVIEW +
    review_flagged (services.py) — the re-approval return path belongs to
    the moderators' queue (see final report note).
    """
    if not new_category or new_category == current_category:
        return False
    return (status == ContentStatus.PUBLISHED.value
            and lifecycle == ListingLifecycle.ACTIVE.value)
