"""Follow models (PROMPTS.md Prompt 13).

Follows are ASYMMETRIC and approval-free: (follower -> followee) is a
one-way subscription, nothing gates it. There is deliberately NO cached
follower-count column — the public number comes only from
services.visible_follower_count(), which hides counts below 10 (the
Prompt 13 privacy floor). A cached counter would hand the raw number to
any table/cache viewer and make the floor unenforceable.
"""
from django.conf import settings
from django.db import models

from apps.core.types import TimestampedModel


class Follow(TimestampedModel):
    """One directed edge in the social graph (follower follows followee)."""

    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="following",  # x.following -> edges where x follows
    )
    followee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="followers",  # x.followers -> edges pointing at x
    )

    class Meta:
        constraints = [
            # WHY unique pair: re-follow cannot duplicate rows — the service
            # layer is idempotent AND the database refuses bad rows even if a
            # future API forgets the house rules.
            models.UniqueConstraint(
                fields=["follower", "followee"], name="uniq_follow_pair"
            ),
            # Defense in depth: a self-edge is never storable (KDPA-minimal
            # graph — a self-follow row is pure noise for the feed later).
            models.CheckConstraint(
                condition=~models.Q(follower=models.F("followee")),
                name="follows_no_self_edge",
            ),
        ]
        indexes = [
            models.Index(fields=["follower", "-created_at"]),
            models.Index(fields=["followee", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.follower} follows {self.followee}"
