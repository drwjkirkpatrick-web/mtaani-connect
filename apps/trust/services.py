"""Trust services (Prompt 16) — one door for votes, rollups, tiers, badges.

PUBLIC API for other modules (parent plugs these in; never import internals):

    from apps.trust import services

    vote, created = services.record_vote(voter=user, target=post_or_answer)
    services.remove_vote(voter=user, target=post_or_answer)          -> bool
    services.rollup_for(user)   # votes RECEIVED: {"GENERAL": {"lifetime": 3,
                                #   "last90": 2}, ...} sorted, for the owner
    services.usefulness_scorer(user, "GENERAL")   # float 0.0..1.0 (Prompt 11)
    services.helpfulness_badges(user)             # current badges [{...}]
    services.all_badge_events(user)               # award history (Prompt 22)
    services.refresh_badges(user)                 # re-run the award hook
    services.category_of(target)                  # trust category of a target
    services.target_is_visible(target)            # not-REMOVED rule

DESIGN DECISIONS (documented for reviewers):
- READ-TIME ROLLUPS (not materialized): HelpfulRollup stays a service,
  not a table. A materialized cache can drift on removals/admin edits, and
  the 90-day window changes under it every day; read-time aggregation over
  one user's vote rows is always right and cheap (a member's helpful-vote
  count is hundreds at most, not a feed query). If Prompt 22's reputation
  needs a cached shape, THAT prompt adds its own materialization.
- BADGES ARE EARNED-ONCE: the award hook (refresh_badges) fires after every
  vote change, evaluates the CURRENT rollup against the TrustTier rows with
  an injectable clock, and writes BadgeEvents for tiers not yet held
  (UNIQUE(user, category, level) stops refires). Falling below a tier later
  never revokes it — trust is something you earned.
- DETERMINISM: any per-row pass (rollup, evaluation) is performed over votes
  ordered by (created_at, pk), and tiers ordered by (min_lifetime, min_90d,
  pk), so tests and replays are deterministic even under ties.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.types import ContentStatus, SafetyCategory

from .models import (
    GENERAL_CATEGORY,
    LEVEL_RANK,
    MSG_TARGET_MISSING,
    MSG_TARGET_NOT_ALLOWED,
    MSG_TARGET_REMOVED,
    MSG_SELF_VOTE,
    ALLOWED_TARGET_APPS,
    BadgeEvent,
    HelpfulVote,
    TrustLevel,
    TrustTier,
)

# HOW LONG the "recent" window is (spec verbatim: last-90-days).
WINDOW_DAYS = 90

# Scorer normalizers — WHAT they are: saturation points of the 0.0..1.0
# usefulness score, NOT award thresholds (those are TrustTier DB rows).
# 30 lifetime and 12 recent helpful votes ≈ a consistently helpful member.
# One constant here, one consumer below — tuning is a documented code change
# on purpose (Prompt 11's ranking should not move with admin threshold edits).
SCORER_LIFETIME_NORM = 30.0
SCORER_WINDOW_NORM = 12.0

# Category display labels — structural display vocabulary only (NOT persona
# content). Unknown future categories fall back to the raw value.
CATEGORY_LABELS = {
    GENERAL_CATEGORY: _("Community knowledge"),
    SafetyCategory.MEDICAL.value: _("Medical handling"),
    SafetyCategory.ELECTRICAL.value: _("Electrical handling"),
    SafetyCategory.CONSTRUCTION.value: _("Construction handling"),
}


def _now(now):
    """The evaluation clock: injected `now` wins, otherwise the real clock."""
    return timezone.now() if now is None else now


def category_label(category: str) -> str:
    """Human label for a category value (used by templates + JSON surfaces)."""
    return str(CATEGORY_LABELS.get(category, category))


def category_of(target) -> str | None:
    """Which trust category a voted target rolls up into (None = foreign).

    Mapping (documented contract, not a table):
      qa.Answer  → its snapshotted safety_category; NONE means everyday
                   knowledge and rolls up as GENERAL.
      posts.Post → GENERAL (posts carry no category field of their own).
    Called at RECORD time; HelpfulVote.category pins the result on the row.
    """
    if target is None:
        return None
    app_label = ContentType.objects.get_for_model(target).app_label
    if app_label not in ALLOWED_TARGET_APPS:
        return None
    if app_label == "qa":
        value = getattr(target, "safety_category", "") or SafetyCategory.NONE.value
        return GENERAL_CATEGORY if value == SafetyCategory.NONE.value else value
    return GENERAL_CATEGORY  # posts.


def target_is_visible(target) -> bool:
    """The vote-time visibility rule: the target must NOT be REMOVED.

    WHY a status read instead of apps.moderation.services.visible(): that
    helper mixes in the viewer's block list (a PERSONAL filter), while a
    vote is about the target itself — one status read is exactly the rule.
    Answers carry no status column of their own; they inherit the question's
    lifecycle (same rule as qa.visible_answers).
    """
    if target is None:
        return False
    status = getattr(target, "status", None)
    if status == ContentStatus.REMOVED.value:
        return False
    question = getattr(target, "question", None)  # answers→question; posts→None
    if question is not None and getattr(
        question, "status", None
    ) == ContentStatus.REMOVED.value:
        return False
    return True


def _validate_vote(voter, target) -> None:
    """Shared pre-checks for record/remove: identity, existence, allow-list."""
    if not getattr(voter, "is_authenticated", False) or voter.pk is None:
        raise ValidationError(_("Log in to mark content helpful."))
    if target is None or target.pk is None:
        raise ValidationError(MSG_TARGET_MISSING)
    if ContentType.objects.get_for_model(target).app_label not in ALLOWED_TARGET_APPS:
        raise ValidationError(MSG_TARGET_NOT_ALLOWED)


@transaction.atomic
def record_vote(*, voter, target, now=None) -> tuple[HelpfulVote, bool]:
    """Mark a post/answer helpful. IDEMPOTENT: one row per (voter, item) —
    voting again is a harmless no-op, not a stacked vote.

    Gates (service-level, so admin/scripts/future APIs inherit them): the
    target must be a posts/qa object, must not be REMOVED, and must not be
    the voter's own content (self-votes inflate a trust rollup and are
    useless by definition — helpfulness is what OTHERS find helpful).
    `now` injects the badge-evaluation clock (tests / deterministic replays).

    HELPFULNESS ACCRUES TO THE AUTHOR: the row pins `owner` (the target's
    author); the award hook below fires for the OWNER, so a helpful member
    is the one whose content keeps getting marked.
    Returns (vote, created) — created=False means the vote was already there.
    """
    _validate_vote(voter, target)
    # WHY author_id (no .author dereference): both targets are FK models, so
    # the id is on the instance already — no extra query for the check.
    owner_id = getattr(target, "author_id", None)
    if owner_id is None:
        raise ValidationError(MSG_TARGET_MISSING)  # no author → no recipient
    if owner_id == voter.pk:
        raise ValidationError(MSG_SELF_VOTE)
    if not target_is_visible(target):
        raise ValidationError(MSG_TARGET_REMOVED)
    category = category_of(target)
    if category is None:
        raise ValidationError(MSG_TARGET_NOT_ALLOWED)
    vote, created = HelpfulVote.objects.get_or_create(
        voter=voter,
        owner_id=owner_id,
        category=category,
        target_ct=ContentType.objects.get_for_model(target),
        target_id=target.pk,
    )
    if created:
        # The award hook runs INSIDE the vote transaction for the OWNER: a
        # badge row and the crossing vote commit together or not at all.
        _award_badges_for_owner_id(owner_id, now=now)
    return vote, created


@transaction.atomic
def remove_vote(*, voter, target) -> bool:
    """Revoke a helpful vote (spec: revocable). True when a row was deleted;
    removing a missing vote is a harmless no-op (idempotent, like qa).

    WHY no badge refresh here: badges are earned-once (see module docstring),
    and lowering a rollup can never CROSS a threshold upward.
    """
    _validate_vote(voter, target)
    deleted, _ = HelpfulVote.objects.filter(
        voter=voter,
        target_ct=ContentType.objects.get_for_model(target),
        target_id=target.pk,
    ).delete()
    return deleted > 0


def rollup_for(user, *, now=None) -> dict[str, dict[str, int]]:
    """Per-category helpfulness counts a member has EARNED — helpful votes
    RECEIVED on their posts/answers (the helpful user is the owner).

    Returns {"<category>": {"lifetime": int, "last90": int}, ...} with
    categories in sorted (= deterministic, tie-break-friendly) order.
    `now` is the clock the 90-day window is measured against (injected in
    tests; real clock at read time). Votes already cast on content that was
    LATER removed still count — trust already earned is not revoked
    retroactively (moderation takes the content down; Prompt 22 may refine).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return {}
    # (created_at, pk) ordering: the tie-break contract — equal timestamps
    # replay in pk order, so every consumer (incl. tests' oracle replay)
    # walks the stream identically. owner= because help is earned by receipt.
    votes = list(
        HelpfulVote.objects.filter(owner=user).order_by("created_at", "pk")
    )
    if not votes:
        return {}

    # WHY no cross-app lookups here: each row carries the category pinned at
    # vote time (models.HelpfulVote.category) — one-user aggregation is one
    # query on this app's own table, however big that module grows.
    now = _now(now)
    window_start = now - timedelta(days=WINDOW_DAYS)
    rollup: dict[str, dict[str, int]] = {}
    for vote in votes:
        bucket = rollup.setdefault(vote.category, {"lifetime": 0, "last90": 0})
        bucket["lifetime"] += 1
        if vote.created_at >= window_start:
            bucket["last90"] += 1
    return {category: rollup[category] for category in sorted(rollup)}


def tiers_for(category: str) -> list[TrustTier]:
    """Tier rows of one category in the deterministic evaluation order."""
    return list(
        TrustTier.objects.filter(category=category).order_by(
            "min_lifetime", "min_90d", "pk"
        )
    )


def _award_badges_for_owner_id(owner_id: int, *, now=None) -> list[BadgeEvent]:
    """Internal hook entry: the vote knows the owner's pk only, the award
    pipeline speaks in User objects — resolve the one row and delegate."""
    from django.contrib.auth import get_user_model

    return refresh_badges(get_user_model().objects.get(pk=owner_id), now=now)


@transaction.atomic
def refresh_badges(user, *, now=None) -> list[BadgeEvent]:
    """THE AWARD HOOK — evaluate current rollups against tiers, award newly
    crossed levels once. Called (inside the vote transaction) after votes
    change; also callable directly (admin re-runs, self-healing, tests).

    `now` is the injected evaluation clock (PROMPTS.md hard rule: time-
    dependent logic is injectable). Returns the NEWLY awarded events in the
    deterministic evaluation order: categories sorted, then tiers by
    (min_lifetime, min_90d, pk) — creation pk order is the tie-break record.
    """
    now = _now(now)
    newly: list[BadgeEvent] = []
    rollups = rollup_for(user, now=now)
    for category in sorted(rollups):
        counts = rollups[category]
        for tier in tiers_for(category):
            if (
                counts["lifetime"] >= tier.min_lifetime
                and counts["last90"] >= tier.min_90d
            ):
                # get_or_create on the UNIQUE (user, category, level) row:
                # idempotent under races, never refires an existing badge.
                event, created = BadgeEvent.objects.get_or_create(
                    user_id=user.pk,
                    category=category,
                    level=tier.level,
                    defaults={"awarded_at": now},
                )
                if created:
                    newly.append(event)
    return newly


def helpfulness_badges(user) -> list[dict]:
    """Current badges for display surfaces (profiles Prompt 07, answers,
    future Prompt 22): the HIGHEST held level per category.

    Returns a list of plain dicts (never model internals), sorted by
    category: {category, category_label, level, label, awarded_at}.
    Consumers ({% include %} apps/trust/templates/trust/_badges.html) get
    everything they render; other apps (feed ranking, reputation) use the
    scorer/rollup, so this list stays display-shaped on purpose.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    events = BadgeEvent.objects.filter(
        user=user
    ).order_by("awarded_at", "category", "level", "pk")
    tops: dict[str, dict] = {}
    for event in events:
        level_value = event.level
        top = tops.get(event.category)
        if top is None or LEVEL_RANK[level_value] > LEVEL_RANK[top["level"]]:
            tops[event.category] = {
                "category": event.category,
                "category_label": category_label(event.category),
                "level": level_value,
                "label": str(TrustLevel(level_value).label),
                "awarded_at": event.awarded_at,
            }
    return [tops[category] for category in sorted(tops)]


def all_badge_events(user) -> list[dict]:
    """Full badge AWARD HISTORY (Prompt 22 provenance), replay order:
    (awarded_at, category, level rank, pk) — deterministic under ties."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    events = BadgeEvent.objects.filter(
        user=user
    ).order_by("awarded_at", "category", "level", "pk")
    return [
        {
            "category": event.category,
            "category_label": category_label(event.category),
            "level": event.level,
            "label": str(TrustLevel(event.level).label),
            "awarded_at": event.awarded_at,
        }
        for event in events
    ]


def usefulness_scorer(user, category, *, now=None) -> float:
    """Normalized 0.0..1.0 usefulness of one member in one category.

    Prompt 11's feed reads THIS (never the rollup dict directly):
        score = 0.6·min(1, lifetime/SCORER_LIFETIME_NORM)
              + 0.4·min(1, last90/SCORER_WINDOW_NORM)
    Pure arithmetic over rollup_for — deterministic (same rows+clock → same
    float, rounded to 4dp), monotone in both counts, capped at 1.0, and 0.0
    for members the category has never benefited from. It needs no badge to
    exist, so a first-time helper is ranked before their first award.
    """
    counts = (rollup_for(user, now=now) or {}).get(category) or {
        "lifetime": 0,
        "last90": 0,
    }
    lifetime_part = min(1.0, counts["lifetime"] / SCORER_LIFETIME_NORM)
    window_part = min(1.0, counts["last90"] / SCORER_WINDOW_NORM)
    return round(0.6 * lifetime_part + 0.4 * window_part, 4)
