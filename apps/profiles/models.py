"""Profile model (Prompt 07).

KDPA notes baked into the shape:
- location is a WARD LABEL, never coordinates — nothing here can leak
  a precise position because the field cannot hold one
- `precise_location_consent` exists ONLY as an explicit, auditable toggle;
  NOTHING in V1 consumes it (transport passengers never share GPS in V1)
- `is_verified_seller` is optional identity verification (brief §5) —
  default off, ordinary users never need it
"""
from django.conf import settings
from django.db import models

from apps.core.types import LocationVisibility, TimestampedModel

VISIBILITY_LEVELS = ("public", "members", "private")
# The profile field groups privacy settings can gate (Prompt 07).
GROUPS = ("bio", "skills", "interests", "location")
DEFAULT_FIELD_VISIBILITY = {
    "bio": "public",
    "skills": "public",
    "interests": "members",
    "location": "public",
}


class Profile(TimestampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    display_name = models.CharField(max_length=64, blank=True, default="")
    bio = models.TextField(blank=True, default="")
    skills = models.JSONField(default=list, blank=True)
    interests = models.JSONField(default=list, blank=True)
    ward = models.CharField(max_length=64, blank=True, default="")

    location_visibility = models.CharField(
        max_length=16,
        choices=LocationVisibility.choices,
        default=LocationVisibility.WARD,
    )
    # per-field-group visibility: public / members / private (contract 07)
    field_visibility = models.JSONField(default=dict, blank=True)

    # WHY stored but unconsumed: the consent must be *visible and auditable*
    # (KDPA), not silently implied. Any future feature needing precise
    # location MUST read this flag and nothing else.
    precise_location_consent = models.BooleanField(default=False)

    avatar = models.ForeignKey(
        "media.MediaRef", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="profile_avatars",
    )

    is_verified_seller = models.BooleanField(default=False)

    def visibility_for(self, group: str) -> str:
        return self.field_visibility.get(group, DEFAULT_FIELD_VISIBILITY.get(group, "public"))
