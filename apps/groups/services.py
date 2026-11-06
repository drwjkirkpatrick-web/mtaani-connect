"""Group services (PROMPTS.md Prompt 13).

Membership + role rules live HERE so views and future apps cannot bypass
them: the owner can never leave their own group, joining never duplicates or
downgrades a role, and every moderation decision combines the account-level
staff hook from accounts with the in-group ladder.

THE PUBLIC API FOR OTHER APPS (cross-app reads go through these ONLY):
    member_group_ids(user)      — THE one-call feed hook (Prompt 11)
    group_tier_posts(user)      — placeholder until posts app (Prompt 10)
    create_group / join / leave — membership mutations
    user_role / is_group_owner / is_group_member / is_group_moderator
"""
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.translation import gettext as _

from apps.core.types import GroupKind

from .models import MAX_DESCRIPTION_CHARS, Group, GroupMembership, GroupRole


class OwnerCannotLeave(Exception):
    """The owner cannot leave their own group — transfer ownership or
    delete the group instead (a group must always have an active owner)."""


# ---- role helpers (accounts staff hook + in-group ladder) -------------------

def user_role(user, group) -> GroupRole | None:
    """The user's role in `group`, or None when not a member (safe for
    anonymous users)."""
    if not getattr(user, "is_authenticated", False):
        return None
    membership = GroupMembership.objects.filter(user=user, group=group).first()
    return GroupRole(membership.role) if membership else None


def is_group_owner(user, group) -> bool:
    return user_role(user, group) is GroupRole.OWNER


def is_group_member(user, group) -> bool:
    return user_role(user, group) is not None


def is_group_moderator(user, group) -> bool:
    """True when `user` may moderate group content (Prompt 10 will call this
    on every post surface).

    WHY this shape: it REUSES the moderation role hook from accounts —
    `user.is_staff`, the same hook apps/profiles gates seller verification
    with — instead of inventing a new account-level flag. In-group, the
    ladder says owner and moderator both moderate; a plain member never does.
    """
    if user and getattr(user, "is_staff", False):
        return True
    return user_role(user, group) in (GroupRole.OWNER, GroupRole.MODERATOR)


# ---- membership mutations ---------------------------------------------------

def create_group(
    *, owner, name: str, kind: str, ward: str = "", description: str = ""
) -> Group:
    """Create a group plus the owner's OWNER membership in ONE transaction —
    a group can never exist without its owner membership row."""
    if not (name or "").strip():
        raise ValidationError(_("Give your group a name."))
    if len(description or "") > MAX_DESCRIPTION_CHARS:
        raise ValidationError(_("Description must be 500 characters or fewer."))
    try:
        GroupKind(kind)
    except ValueError:
        raise ValidationError(_("Pick a valid group type.")) from None

    group = Group.objects.create(
        owner=owner,
        name=name.strip(),
        kind=kind,
        ward=(ward or "").strip(),
        description=description or "",
    )
    GroupMembership.objects.create(group=group, user=owner, role=GroupRole.OWNER)
    return group


def join(*, user, group) -> tuple[GroupMembership, bool]:
    """Idempotent join: returns (membership, created). Joining twice never
    duplicates a row, and never DOWNGRADES a role (the owner re-clicking
    join stays owner, a moderator stays moderator)."""
    membership, created = GroupMembership.objects.get_or_create(
        user=user, group=group, defaults={"role": GroupRole.MEMBER}
    )
    return membership, created


def leave(*, user, group) -> bool:
    """Idempotent leave. OwnerCannotLeave is raised for the owner (a group
    must keep its owner); a non-member leaving is a no-op returning False,
    never an error."""
    if is_group_owner(user, group):
        raise OwnerCannotLeave(_("The group owner cannot leave their own group."))
    # NOTE: the unpack target must NOT be `_` — this function uses gettext `_`
    # while raising above; a second local `_` would shadow it mid-function.
    deleted, _rel = GroupMembership.objects.filter(user=user, group=group).delete()
    return deleted > 0


def members_of(group) -> models.QuerySet:
    """Membership rows to page over (each row carries its user)."""
    return (
        GroupMembership.objects.filter(group=group)
        .select_related("user")
        .order_by("user__username")
    )


def member_count(group) -> int:
    return GroupMembership.objects.filter(group=group).count()


# ---- the feed integration (Prompt 11's ONE function call) -------------------

def member_group_ids(user) -> list[int]:
    """THE feed hook: ids of groups `user` belongs to. Prompt 11's feed
    assembles its local-relevance tier with exactly one call to this —
    integration must stay one function call forever (Prompt 13 acceptance).
    Anonymous -> [] so every tier degrades cleanly for signed-out visitors."""
    if not getattr(user, "is_authenticated", False):
        return []
    return list(
        GroupMembership.objects.filter(user=user).values_list("group_id", flat=True)
    )


def group_tier_posts(user) -> list:
    """PLACEHOLDER for group posts — the posts app lands in Prompt 10.
    Currently returns [] honestly. NOTE: Prompt 11's feed will combine this
    with member_group_ids(user) above; do NOT let the feed inner-join group
    tables directly (cross-app rule: public functions only)."""
    return []
