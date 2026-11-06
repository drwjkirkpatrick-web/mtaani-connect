"""Prompt 13 acceptance — FOLLOWS: asymmetric graph, idempotent mutations,
and the below-10 follower-count privacy floor (the helper itself AND every
rendered surface must obey it; the raw count must never leak).

WHY: PROMPTS.md Prompt 13 hides follower counts below 10 so pile-on culture
is chilled and bought-follower counts look exactly like real small accounts.
"""
import inspect

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.core.testing import assert_under_budget
from apps.follows import services
from apps.follows.models import Follow

pytestmark = pytest.mark.django_db

User = get_user_model()


def make_user(name: str):
    return User.objects.create_user(username=name, password="pass-12345")


# ---- follow/unfollow idempotency --------------------------------------------

def test_follow_creates_directional_row(fresh_user):
    other = make_user("amina")
    assert services.follow(follower=fresh_user, followee=other) is True
    row = Follow.objects.get(follower=fresh_user, followee=other)
    assert row.followee_id == other.pk
    # ASYMMETRY check: following someone does not make them follow you, and
    # there is no approval/pending state — a second row only exists if they act.
    assert not Follow.objects.filter(follower=other, followee=fresh_user).exists()


def test_follow_is_idempotent(fresh_user):
    other = make_user("amina")
    assert services.follow(follower=fresh_user, followee=other) is True
    assert services.follow(follower=fresh_user, followee=other) is True  # no-op re-follow
    assert Follow.objects.filter(follower=fresh_user, followee=other).count() == 1


def test_unfollow_twice_idempotent(fresh_user):
    other = make_user("amina")
    services.follow(follower=fresh_user, followee=other)
    assert services.unfollow(follower=fresh_user, followee=other) is True
    assert services.unfollow(follower=fresh_user, followee=other) is False  # no-op, no error
    assert Follow.objects.count() == 0


def test_unfollow_never_followed_is_noop(fresh_user):
    other = make_user("amina")
    assert services.unfollow(follower=fresh_user, followee=other) is False


def test_self_follow_rejected_by_service_and_database(fresh_user):
    with pytest.raises(services.SelfFollow):
        services.follow(follower=fresh_user, followee=fresh_user)
    # Defense in depth: the DB refuses a self-edge even without the service.
    with pytest.raises(IntegrityError), transaction.atomic():
        Follow.objects.create(follower=fresh_user, followee=fresh_user)


def test_lists_read_the_right_direction(fresh_user):
    a, b = make_user("amina"), make_user("brian")
    services.follow(follower=a, followee=fresh_user)
    services.follow(follower=fresh_user, followee=b)
    assert [row.follower for row in services.followers_of(fresh_user)] == [a]
    assert [row.followee for row in services.following_of(fresh_user)] == [b]
    assert set(services.followed_user_ids(fresh_user)) == {b.pk}
    assert services.followed_user_ids(None) == []  # anonymous: no graph


def test_is_following_is_safe_for_anonymous():
    from django.contrib.auth.models import AnonymousUser

    assert services.is_following(follower=AnonymousUser(), followee=None) is False


# ---- below-10 follower-count privacy floor -----------------------------------

def _give_followers(target, count):
    fans = []
    for i in range(count):
        fan = make_user(f"fan{i:02d}")
        services.follow(follower=fan, followee=target)
        fans.append(fan)
    return fans


def test_follower_count_hidden_below_ten(fresh_user):
    _give_followers(fresh_user, 9)
    assert services.visible_follower_count(fresh_user) is None


def test_follower_count_shown_at_ten_and_back(fresh_user):
    fans = _give_followers(fresh_user, 10)
    assert services.visible_follower_count(fresh_user) == 10
    assert services.unfollow(follower=fans[0], followee=fresh_user) is True
    # Crossing back under the floor hides the number again — no half states.
    assert services.visible_follower_count(fresh_user) is None


def test_followers_page_hides_raw_count_below_ten(client, fresh_user):
    _give_followers(fresh_user, 4)
    response = client.get(reverse("follows:followers", args=[fresh_user.pk]))
    assert response.status_code == 200
    # Context-level guarantee: only the gated value exists on the page.
    assert response.context["follower_count"] is None
    content = response.content.decode()
    assert "Growing" in content  # the honest hidden state
    # The raw count must never leak via ANY template rendering.
    assert "4 follower" not in content and "4 followers" not in content


def test_followers_page_shows_count_at_ten_for_any_viewer(client, fresh_user):
    _give_followers(fresh_user, 10)
    response = client.get(reverse("follows:followers", args=[fresh_user.pk]))
    assert response.context["follower_count"] == 10
    assert "10 followers" in response.content.decode()


def test_follow_unfollow_via_post_routes(client_auth, fresh_user):
    other = make_user("amina")
    landing = reverse("follows:followers", args=[other.pk])
    response = client_auth.post(
        reverse("follows:follow", args=[other.pk]), {"next": landing}
    )
    assert response.status_code == 302
    assert services.is_following(follower=fresh_user, followee=other)
    # Re-POST stays followed (idempotent at the route level too).
    client_auth.post(reverse("follows:follow", args=[other.pk]), {"next": landing})
    assert services.is_following(follower=fresh_user, followee=other)
    assert (
        client_auth.post(
            reverse("follows:unfollow", args=[other.pk]), {"next": landing}
        ).status_code
        == 302
    )
    assert not services.is_following(follower=fresh_user, followee=other)


def test_follow_route_requires_login_and_post(client, fresh_user):
    other = make_user("amina")
    # Anonymous POST is bounced to login before any state change.
    response = client.post(reverse("follows:follow", args=[other.pk]))
    assert response.status_code == 302
    assert not services.is_following(follower=fresh_user, followee=other)
    # Authenticated GET must not mutate anything (POST-only route).
    client.force_login(fresh_user)
    assert client.get(reverse("follows:follow", args=[other.pk])).status_code == 405


def test_list_pages_public_paginated_and_budgeted(client, fresh_user):
    for i in range(25):
        services.follow(follower=fresh_user, followee=make_user(f"mate{i:02d}"))
    following_url = reverse("follows:following", args=[fresh_user.pk])
    page_one = client.get(following_url)
    assert page_one.status_code == 200
    assert len(page_one.context["rows"]) == 20  # HARD RULE: ≤20 items/page
    page_two = client.get(following_url + "?page=2")
    assert len(page_two.context["rows"]) == 5
    assert "mate24" in page_one.content.decode() and "mate00" in page_two.content.decode()
    # Mobile budget (PROMPTS.md hard rule): both pages ≤ 50 KB.
    assert_under_budget(page_one.content.decode(), kb=50)
    assert_under_budget(page_two.content.decode(), kb=50)


def test_followers_page_budget(client, fresh_user):
    _give_followers(fresh_user, 20)
    response = client.get(reverse("follows:followers", args=[fresh_user.pk]))
    assert response.status_code == 200
    assert_under_budget(response.content.decode(), kb=50)


def test_followed_user_ids_signature_is_feed_contract():
    # Prompt 11 reads follows through this hook — keep its shape stable.
    sig = inspect.signature(services.followed_user_ids)
    assert list(sig.parameters) == ["user"]
