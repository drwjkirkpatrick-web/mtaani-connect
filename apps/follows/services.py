"""Follow services (PROMPTS.md Prompt 13).

WHY a service layer: idempotency AND the below-10 follower-count privacy
floor live in exactly ONE place. Views, future APIs and Prompt 11's feed
must all come through these functions — none of them may compute a raw
follower count itself.

THE PUBLIC API FOR OTHER APPS (cross-app reads go through these ONLY):
    follow / unfollow / is_following   — membership mutations + check
    followers_of / following_of        — directed-edge querysets (read)
    followed_user_ids(user)            — ids for Prompt 11's "followed" tier
    visible_follower_count(user)       — the ONLY sanctioned follower count
"""
from django.db import models
from django.utils.translation import gettext as _

from .models import Follow

# WHY 10: below-10 counts render as "growing" (no number) — small numbers
# read as "nobody likes this" and fuel pile-on culture; bought-follower
# counts become pointless because every account under the floor looks the
# same (PROMPTS.md Prompt 13).
FOLLOWER_COUNT_FLOOR = 10


class SelfFollow(Exception):
    """A user tried to follow themself (the UI never offers this path)."""


def follow(*, follower, followee) -> bool:
    """Ensure follower --follows--> followee. IDEMPOTENT by contract:
    re-following is a no-op that still returns True (the pair ends up
    connected either way) — no approval step exists in this flow."""
    if follower.pk == followee.pk:
        raise SelfFollow(_("You cannot follow yourself."))
    Follow.objects.get_or_create(follower=follower, followee=followee)
    return True


def unfollow(*, follower, followee) -> bool:
    """Remove follower --follows--> followee if present. IDEMPOTENT by
    contract: unfollowing someone you never followed is a no-op that
    returns False (nothing changed), never an error."""
    # `_rel` not `_`: gettext `_` stays the one binding in this module.
    deleted, _rel = Follow.objects.filter(
        follower=follower, followee=followee
    ).delete()
    return deleted > 0


def is_following(*, follower, followee) -> bool:
    """Directional check; safe for AnonymousUser (feed renders for them)."""
    if not getattr(follower, "is_authenticated", False):
        return False
    return Follow.objects.filter(
        follower=follower, followee=followee
    ).exists()


def followers_of(user) -> models.QuerySet:
    """Edges pointing AT `user` (newest first) for list pages and checks."""
    return (
        Follow.objects.filter(followee=user)
        .select_related("follower")
        .order_by("-created_at")
    )


def following_of(user) -> models.QuerySet:
    """Edges FROM `user` (newest first) for list pages."""
    return (
        Follow.objects.filter(follower=user)
        .select_related("followee")
        .order_by("-created_at")
    )


def followed_user_ids(user) -> list[int]:
    """THE follows-side feed hook (Prompt 11): ids of people `user` follows.
    The feed must go through this, not the internal table (one-app-per-prompt
    cross-app rule). Anonymous -> [] so feed tiers degrade cleanly."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(
        Follow.objects.filter(follower=user).values_list("followee_id", flat=True)
    )


def follower_count(user) -> int:
    """INTERNAL raw count. NOTE: this must never reach a template, JSON
    response or message — only visible_follower_count may leave the module."""
    return Follow.objects.filter(followee=user).count()


def visible_follower_count(user) -> int | None:
    """The ONLY sanctioned follower count for any rendered surface.

    Below FOLLOWER_COUNT_FLOOR returns None — templates render a neutral
    "growing" state, never a number (Prompt 13 privacy floor). At/above the
    floor the real count. The rule applies to EVERY viewer, including the
    user themselves — uniform hiding is what makes it leak-proof.
    """
    count = Follow.objects.filter(followee=user).count()
    return count if count >= FOLLOWER_COUNT_FLOOR else None
