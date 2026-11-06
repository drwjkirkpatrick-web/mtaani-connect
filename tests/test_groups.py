"""Prompt 13 acceptance — GROUPS: membership lifecycle (join/leave idempotent,
owner cannot leave), the role ladder combined with the accounts staff hook,
and the ONE-call feed hook for Prompt 11 (mocked while Prompt 11 not built).

WHY: groups must work standalone AND expose member_group_ids(user) so the
feed's local-relevance tier stays a single function call forever.
"""
import inspect

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.testing import assert_under_budget
from apps.core.types import GroupKind
from apps.groups import services
from apps.groups.models import Group, GroupMembership, GroupRole

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(name: str):
    return User.objects.create_user(username=name, password="pass-12345")


_owner_seq = 0


def _next_owner():
    """A fresh unique owner per call — several groups in ONE test each need
    their own owner row (usernames are unique, per-user create)."""
    global _owner_seq
    _owner_seq += 1
    return make_user(f"grpowner{_owner_seq:03d}")


def make_group(
    owner=None,
    *,
    name: str = "Kawangware Coders",
    kind: str = GroupKind.INTEREST.value,
    ward: str = "Kawangware",
    description: str = "Writing code together.",
):
    return services.create_group(
        owner=owner or _next_owner(), name=name, kind=kind,
        ward=ward, description=description,
    )


# ---- creation + owner membership ---------------------------------------------

def test_create_group_grants_owner_membership():
    owner = make_user("amka")
    group = services.create_group(
        owner=owner, name="Amka Farmers", kind=GroupKind.INTEREST.value,
        ward="Kawangware", description="Sharing farm tips.",
    )
    assert Group.objects.filter(pk=group.pk).exists()
    membership = GroupMembership.objects.get(group=group, user=owner)
    assert membership.role == GroupRole.OWNER


def test_create_group_rejects_bad_kind_and_long_description():
    owner = make_user("amka")
    with pytest.raises(ValidationError):
        services.create_group(owner=owner, name="Bad", kind="VIBES")  # not a GroupKind
    with pytest.raises(ValidationError):
        services.create_group(
            owner=owner, name="Too Much", kind=GroupKind.INTEREST.value,
            description="x" * 501,  # ONE over the ceiling
        )


def test_create_group_via_form(client_auth, fresh_user):
    response = client_auth.post(
        reverse("groups:create"),
        {"name": "Sukuma Wiki Growers", "kind": GroupKind.INTEREST.value,
         "ward": "Kawangware", "description": "Five hundred or fewer."},
    )
    assert response.status_code == 302
    group = Group.objects.get(name="Sukuma Wiki Growers")
    assert services.is_group_owner(fresh_user, group)


def test_create_form_rejects_over_500(client_auth):
    form_response = client_auth.post(
        reverse("groups:create"),
        {"name": "Too Long", "kind": GroupKind.INTEREST.value,
         "description": "x" * 501},
    )
    assert form_response.status_code == 200  # re-rendered with inline errors
    assert Group.objects.count() == 0


# ---- membership lifecycle ------------------------------------------------------

def test_join_adds_member_role(fresh_user):
    group = make_group()
    membership, created = services.join(user=fresh_user, group=group)
    assert created is True and membership.role == GroupRole.MEMBER


def test_join_is_idempotent(fresh_user):
    group = make_group()
    first, _ = services.join(user=fresh_user, group=group)
    second, created = services.join(user=fresh_user, group=group)
    assert created is False and first.pk == second.pk
    assert GroupMembership.objects.filter(user=fresh_user, group=group).count() == 1


def test_join_never_downgrades_role():
    owner = make_user("amka")
    group = make_group(owner=owner)
    membership, created = services.join(user=owner, group=group)
    assert created is False and membership.role == GroupRole.OWNER


def test_member_can_leave(fresh_user):
    group = make_group()
    services.join(user=fresh_user, group=group)
    assert services.leave(user=fresh_user, group=group) is True
    assert not GroupMembership.objects.filter(user=fresh_user, group=group).exists()


def test_moderator_can_leave(fresh_user):
    group = make_group()
    GroupMembership.objects.create(group=group, user=fresh_user, role=GroupRole.MODERATOR)
    assert services.leave(user=fresh_user, group=group) is True


def test_leave_nonmember_is_idempotent_noop(fresh_user):
    group = make_group()
    assert services.leave(user=fresh_user, group=group) is False


def test_owner_cannot_leave_own_group_service():
    owner = make_user("amka")
    group = make_group(owner=owner)
    with pytest.raises(services.OwnerCannotLeave):
        services.leave(user=owner, group=group)
    # The owner's standing is untouched — the group keeps its owner.
    assert services.is_group_owner(owner, group)


def test_owner_cannot_leave_via_view(client_auth, fresh_user):
    group = services.create_group(
        owner=fresh_user, name="Mine", kind=GroupKind.NEIGHBOURHOOD.value
    )
    response = client_auth.post(reverse("groups:leave", args=[group.pk]))
    assert response.status_code == 302  # friendly message, no 500/404
    assert services.is_group_owner(fresh_user, group)  # still the owner


def test_join_leave_view_flow(client_auth, fresh_user):
    group = make_group()
    assert client_auth.post(reverse("groups:join", args=[group.pk])).status_code == 302
    assert services.user_role(fresh_user, group) is GroupRole.MEMBER
    # Idempotent re-join: still one row, still MEMBER.
    client_auth.post(reverse("groups:join", args=[group.pk]))
    assert GroupMembership.objects.filter(user=fresh_user, group=group).count() == 1
    assert client_auth.post(reverse("groups:leave", args=[group.pk])).status_code == 302
    assert services.user_role(fresh_user, group) is None


def test_write_routes_require_login_and_post(client, fresh_user):
    group = make_group()
    # Anonymous POST: bounced to login before any state change (join + create).
    assert client.post(reverse("groups:join", args=[group.pk])).status_code == 302
    assert client.post(reverse("groups:create"), {"name": "Anon"}).status_code == 302
    assert Group.objects.count() == 1  # untouched
    assert GroupMembership.objects.count() == 1  # owner only
    # Authenticated GET must not mutate anything (POST-only route): login
    # runs before require_POST, so an authenticated visitor alone sees the 405.
    client.force_login(fresh_user)
    assert client.get(reverse("groups:join", args=[group.pk])).status_code == 405
    assert client.get(reverse("groups:leave", args=[group.pk])).status_code == 405


# ---- role ladder + accounts staff hook ------------------------------------------

def test_role_ladder_and_staff_hook():
    owner = make_user("amka")
    group = make_group(owner=owner)
    member = services.join(user=make_user("kip"), group=group)[0]
    moderator = make_user("jina")
    GroupMembership.objects.create(group=group, user=moderator, role=GroupRole.MODERATOR)
    outsider = make_user("geni")
    staff = make_user("staffmx")
    staff.is_staff = True
    staff.save()

    assert services.user_role(member.user, group) is GroupRole.MEMBER
    assert services.is_group_owner(owner, group)
    assert services.is_group_moderator(owner, group)       # in-group owner
    assert services.is_group_moderator(moderator, group)   # in-group moderator
    # The moderation role hook FROM accounts: staff moderates everywhere.
    assert services.is_group_moderator(staff, group)
    assert services.is_group_moderator(member.user, group) is False
    assert services.is_group_moderator(outsider, group) is False
    assert services.is_group_moderator(AnonymousUser(), group) is False
    assert services.user_role(outsider, group) is None
    assert services.is_group_member(member.user, group) is True
    assert services.is_group_member(outsider, group) is False


# ---- read pages -----------------------------------------------------------------

def test_group_list_filters_by_kind(client):
    interest = make_group(name="Chess Club", kind=GroupKind.INTEREST.value)
    make_group(name="Kawangware Estate", kind=GroupKind.NEIGHBOURHOOD.value)
    listing = client.get(reverse("groups:list"))
    assert listing.status_code == 200
    assert len(listing.context["groups"]) == 2
    filtered = client.get(reverse("groups:list"), {"kind": GroupKind.INTEREST.value})
    assert [g.name for g in filtered.context["groups"]] == [interest.name]
    bogus = client.get(reverse("groups:list"), {"kind": "VIBES"})
    assert len(bogus.context["groups"]) == 0  # empty page, never a 500


def test_group_detail_public_shows_description_and_members(client, fresh_user):
    group = make_group(name="Beekeepers", description="Hives and honey.", ward="Kawangware")
    services.join(user=fresh_user, group=group)
    response = client.get(reverse("groups:detail", args=[group.pk]))
    assert response.status_code == 200
    content = response.content.decode()
    assert "Beekeepers" in content
    assert "Hives and honey." in content
    assert "wanjiku" in content and group.owner.username in content
    # Anonymous visitors see no membership action buttons.
    assert "Join group" not in content


def test_detail_shows_action_for_members(client_auth, fresh_user):
    group = make_group()
    client_auth.post(reverse("groups:join", args=[group.pk]))
    content = client_auth.get(reverse("groups:detail", args=[group.pk])).content.decode()
    assert "Leave group" in content          # member sees the leave button
    assert "Join group" not in content


def test_group_detail_pages_at_20_and_under_budget(client):
    owner = make_user("amka")
    group = make_group(owner=owner, name="Ward Watch")
    for i in range(25):
        services.join(user=make_user(f"mate{i:02d}"), group=group)
    # 26 members total (owner + 25 joins)
    detail_url = reverse("groups:detail", args=[group.pk])
    page_one = client.get(detail_url)
    assert len(page_one.context["memberships"]) == 20
    page_two = client.get(detail_url + "?page=2")
    assert len(page_two.context["memberships"]) == 6
    assert_under_budget(page_one.content.decode(), kb=50)


def test_group_list_budget_with_many_groups(client):
    owner = make_user("amka")
    for i in range(24):
        services.create_group(
            owner=owner, name=f"Ward number {i:02d} group",
            kind=GroupKind.NEIGHBOURHOOD.value,
        )
    listing = client.get(reverse("groups:list"))
    assert listing.status_code == 200
    assert len(listing.context["groups"]) == 20  # hard pagination ceiling
    assert_under_budget(listing.content.decode(), kb=50)


# ---- feed integration (Prompt 11's interface, mocked until 11 lands) -------------

def test_member_group_ids_for_feeder(fresh_user):
    owner = make_user("amka")
    one = services.create_group(owner=owner, name="One", kind=GroupKind.INTEREST.value)
    two = services.create_group(owner=owner, name="Two", kind=GroupKind.NEIGHBOURHOOD.value)
    services.join(user=fresh_user, group=one)
    services.join(user=fresh_user, group=two)
    assert set(services.member_group_ids(fresh_user)) == {one.pk, two.pk}
    # Non-members get nothing: the tier is membership-based, not public-wide.
    assert services.member_group_ids(make_user("geni")) == []


def test_member_group_ids_anonymous_and_none():
    make_group()
    assert services.member_group_ids(AnonymousUser()) == []
    assert services.member_group_ids(None) == []


def test_feed_hook_contract_signature():
    # THE contract (Prompt 13 acceptance): feed integration is one function
    # call — member_group_ids(user) — so its signature may not drift.
    signature = inspect.signature(services.member_group_ids)
    assert list(signature.parameters) == ["user"]
    assert signature.parameters["user"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_feed_local_tier_contract_mocked(fresh_user):
    # Prompt 11 is not built yet: mock its assembler EXACTLY at the interface
    # it must consume (member_group_ids + the group-posts placeholder) and
    # prove our hooks satisfy it without touching group tables directly.
    class MockFeedLocalTier:
        """Stands in for Prompt 11's feed tier (deterministic, no DB writes)."""

        def local_tier(self, feed_user):
            return {
                "group_ids": services.member_group_ids(feed_user),
                "posts": services.group_tier_posts(feed_user),
            }

    tier = MockFeedLocalTier().local_tier(fresh_user)
    assert tier["posts"] == []  # placeholder: posts app lands in Prompt 10
    assert isinstance(tier["group_ids"], list)
    # Same for a signed-out visitor — tiers degrade to empty, never error.
    anon_tier = MockFeedLocalTier().local_tier(AnonymousUser())
    assert anon_tier == {"group_ids": [], "posts": []}
