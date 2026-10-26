"""Profile serialization + privacy rules (Prompt 07).

THE ALLOW-LIST IS THE PRIVACY GUARANTEE (PROMPTS.md pitfall): public_data
builds a dict with a FIXED key set from an explicit allow-list. A serializer
that dumps the model would leak every future private field; a new model field
can only reach the public surface by being added here — and the strict
key-set test fails until the author justifies it.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from .models import Profile

# The allow-list. Nothing else may appear in public_data output.
PUBLIC_KEYS = (
    "username", "display_name", "member_since", "is_verified_seller",
    "avatar_thumb", "bio", "skills", "interests", "location",
)

# Location granularity ladder (LocationVisibility): coarse-by-default.
def _location_label(profile: Profile) -> str | None:
    if profile.visibility_for("location") == "private" or not profile.ward:
        return None
    if profile.location_visibility == "WARD":
        return profile.ward
    if profile.location_visibility == "NEIGHBOURHOOD":
        return "Nairobi"
    return "Kenya"  # CITY: nothing more precise than the country


def _visible(profile: Profile, group: str, viewer_is_member: bool) -> bool:
    level = profile.visibility_for(group)
    if level == "private":
        return False
    if level == "members":
        return viewer_is_member
    return True


def public_data(profile: Profile, *, viewer_is_member: bool) -> dict:
    """The ONLY shape rendered on public profile surfaces."""
    user = profile.user
    data = {
        "username": user.username,
        "display_name": profile.display_name or user.username,
        "member_since": profile.created_at.date(),
        "is_verified_seller": profile.is_verified_seller,
        "avatar_thumb": profile.avatar.thumb_path if profile.avatar else None,
        "bio": profile.bio if _visible(profile, "bio", viewer_is_member) else None,
        "skills": profile.skills if _visible(profile, "skills", viewer_is_member) else None,
        "interests": profile.interests if _visible(profile, "interests", viewer_is_member) else None,
        "location": _location_label(profile) if _visible(profile, "location", viewer_is_member) else None,
    }
    assert set(data) == set(PUBLIC_KEYS)  # hard allow-list, guarded by test too
    return data


def get_profile(user) -> Profile:
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def set_verified_seller(*, profile: Profile, by_user) -> None:
    """Human gate: only staff may verify sellers (brief §5 — optional, never
    self-serve). Raises PermissionDenied for anyone else."""
    if not (by_user and by_user.is_staff):
        raise PermissionDenied("Only staff can verify sellers.")
    profile.is_verified_seller = True
    profile.save(update_fields=["is_verified_seller", "updated_at"])
