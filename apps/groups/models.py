"""Group models (PROMPTS.md Prompt 13).

For now a group is metadata + membership ONLY. Group posts do not exist yet
(the posts app lands in Prompt 10; the feed lands in Prompt 11) — the feed's
local-relevance tier will call services.member_group_ids(user), which is the
whole cross-app integration.

WHY the role enum is here and not in apps/core: GroupRole is THIS app's
vocabulary (owner/moderator/member within one group). GroupKind is the
shared taxonomy and already lives in apps/core/types.py — reuse it, never
copy it. Store .value strings only (labels are translatable, values frozen).
"""
from django.conf import settings
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import GroupKind, TimestampedModel

MAX_DESCRIPTION_CHARS = 500  # Prompt 13 spec: description <= 500 characters


class GroupRole(models.TextChoices):
    """The in-group ladder. NOTE: account-level moderation (is_staff today,
    Prompt 15 roles later) is NOT modelled here — services.is_group_moderator
    combines both hooks without inventing new account-level flags."""

    OWNER = "owner", _("Owner")
    MODERATOR = "moderator", _("Moderator")
    MEMBER = "member", _("Member")


class Group(TimestampedModel):
    """A named gathering around kind (interest / neighbourhood / profession).

    KDPA note: `ward` is a coarse plain-text label exactly like Profile.ward —
    this field cannot hold coordinates or phone numbers.
    """

    name = models.CharField(_("Name"), max_length=120)
    kind = models.CharField(_("Kind"), max_length=16, choices=GroupKind.choices)
    ward = models.CharField(_("Ward"), max_length=64, blank=True, default="")
    description = models.TextField(
        _("Description"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(MAX_DESCRIPTION_CHARS)],
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,  # WHY cascade: deletion rights (KDPA) — a
        # user is always deletable; the groups they own go with them.
        related_name="owned_groups",
    )

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["kind", "name"])]

    def __str__(self):
        return self.name


class GroupMembership(TimestampedModel):
    """One user's standing in one group — role ladder via `role`.

    WHY rows and not flags on User: membership is per-group state; a row per
    (user, group) keeps joins idempotent (unique constraint) and lets
    Prompt 10/11 gate content by role without user-table coupling.
    """

    group = models.ForeignKey(
        Group, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="group_memberships",
    )
    role = models.CharField(
        _("Role"), max_length=16, choices=GroupRole.choices, default=GroupRole.MEMBER
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "group"], name="uniq_group_membership_user_group"
            )
        ]
        indexes = [models.Index(fields=["group", "-created_at"])]

    def __str__(self):
        return f"{self.user} in {self.group} ({self.role})"
