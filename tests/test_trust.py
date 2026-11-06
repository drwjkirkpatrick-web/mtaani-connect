"""Prompt 16 acceptance: vote idempotency/revocation, rollup math, threshold
crossing emitting badge events with an injected clock, and tie-breaking
determinism — plus the public consumption surface other modules build on
(helpfulness_badges, usefulness_scorer, the _badges.html partial).

SEMANTICS under test: a helpful vote is cast BY one member on another's
post/answer and ACCRUES to the content author (owner). The badge/tie tests do
not trust the service alone: a deterministic oracle — an independent
chronological replay walking the vote stream ORDERED BY (created_at, pk) —
lives here, per the tie-break contract in services.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

import pytest

from apps.core.testing import assert_under_budget, fresh_user, mock_now
from apps.core.types import ContentStatus
from apps.moderation.services import apply_removal
from apps.posts import services as posts_services
from apps.posts.models import Post
from apps.qa import services as qa_services
from apps.trust import services
from apps.trust.models import BadgeEvent, HelpfulVote, TrustLevel, TrustTier

NOW = mock_now()  # 2026-01-15 09:30 UTC (injected evaluation clock)


@pytest.fixture
def other_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="bena", email="bena@example.com", password="***"
    )


@pytest.fixture
def third_user(db):
    User = get_user_model()
    return User.objects.create_user(
        username="chai", email="chai@example.com", password="***"
    )


def _voted_post(owner, voter, now=NOW):
    """One published post by `owner`, voted helpful by `voter`."""
    post = posts_services.create_post(
        author=owner, text="Fresh sukuma wiki near Gikambura road, KSh 30 a bunch."
    )
    services.record_vote(voter=voter, target=post, now=now)
    return post


# ------------------------------------------------------------- idempotency


@pytest.mark.django_db
def test_record_vote_is_idempotent_per_user_per_item(fresh_user, other_user):
    post = posts_services.create_post(author=other_user, text="Tip one.")
    votes = [services.record_vote(voter=fresh_user, target=post)[1] for _ in range(3)]
    assert votes == [True, False, False]  # one row, not three
    assert HelpfulVote.objects.count() == 1


@pytest.mark.django_db
def test_vote_requires_login_and_a_saved_target(fresh_user, other_user):
    post = posts_services.create_post(author=other_user, text="Tip two.")
    with pytest.raises(Exception):
        services.record_vote(voter=AnonymousUser(), target=post)
    unsaved = Post(
        author=other_user, text="never saved", status=ContentStatus.PUBLISHED.value
    )
    with pytest.raises(Exception):
        services.record_vote(voter=fresh_user, target=unsaved)
    assert HelpfulVote.objects.count() == 0


@pytest.mark.django_db
def test_self_vote_is_rejected(fresh_user):
    own_post = posts_services.create_post(author=fresh_user, text="My tip.")
    question = qa_services.ask_question(
        author=fresh_user, title_en="How do I price mandazi?"
    )
    own_answer = qa_services.post_answer(
        question=question, author=fresh_user, body="Sell three for twenty."
    )
    for target in (own_post, own_answer):
        with pytest.raises(Exception):
            services.record_vote(voter=fresh_user, target=target)
    assert HelpfulVote.objects.count() == 0


# ------------------------------------------------ visibility / allow-list


@pytest.mark.django_db
def test_removed_post_cannot_be_voted(fresh_user, other_user):
    post = posts_services.create_post(author=other_user, text="Tip three.")
    assert apply_removal(post) is True  # moderation's public removal door
    with pytest.raises(Exception):
        services.record_vote(voter=fresh_user, target=post)
    client = Client()
    client.force_login(fresh_user)
    assert client.post(reverse("trust:post_vote", args=[post.pk])).status_code == 404
    assert HelpfulVote.objects.count() == 0


@pytest.mark.django_db
def test_answer_under_removed_question_cannot_be_voted(fresh_user, other_user):
    question = qa_services.ask_question(
        author=other_user, title_en="Boda bike fuel price?"
    )
    answer = qa_services.post_answer(
        question=question, author=other_user, body="A full tank is KSh 500 today."
    )
    apply_removal(question)
    with pytest.raises(Exception):
        services.record_vote(voter=fresh_user, target=answer)
    assert HelpfulVote.objects.count() == 0


@pytest.mark.django_db
def test_targets_are_limited_to_posts_and_qa(fresh_user, other_user):
    from apps.guides.models import GuideRevision
    from apps.guides.services import create_guide

    guide = create_guide(
        author=other_user, title_en="Keep chickens", category="farming",
        skill_level="beginner", steps=[{"position": 1, "text": "Morning feed"}],
    )
    revision = GuideRevision.objects.filter(guide=guide).first()
    with pytest.raises(Exception):
        services.record_vote(voter=fresh_user, target=revision)
    assert HelpfulVote.objects.count() == 0
    assert services.category_of(revision) is None  # foreign app → no category


# ------------------------------------------------------------- revocation


@pytest.mark.django_db
def test_remove_vote_revokes_and_revote_recasts(fresh_user, other_user):
    post = _voted_post(other_user, fresh_user)
    assert services.rollup_for(other_user)["GENERAL"]["lifetime"] == 1
    assert services.rollup_for(fresh_user) == {}  # the voter earns nothing
    assert services.remove_vote(voter=fresh_user, target=post) is True
    assert services.remove_vote(voter=fresh_user, target=post) is False
    assert services.rollup_for(other_user) == {}  # revocation empties the rollup
    vote, created = services.record_vote(voter=fresh_user, target=post)
    assert created is True  # a revoked vote can be cast on the item again


@pytest.mark.django_db
def test_undo_endpoint_revokes_the_vote(client_auth, fresh_user, other_user):
    post = _voted_post(other_user, fresh_user)
    url = reverse("trust:post_vote", args=[post.pk])
    assert client_auth.post(url, {"undo": "1"}).status_code == 302
    assert HelpfulVote.objects.count() == 0


# ------------------------------------------------------- endpoint contract


@pytest.mark.django_db
def test_post_vote_endpoint_needs_login_and_is_idempotent(
    client_auth, fresh_user, other_user
):
    post = posts_services.create_post(author=other_user, text="Tip four.")
    url = reverse("trust:post_vote", args=[post.pk])
    # Anonymous: bounced to login, no vote cast (standalone Client() — the
    # pytest-django `client` fixture is the SAME object client_auth logs in,
    # so a signature with both would silently test logged-in traffic).
    anonymous = Client()
    assert anonymous.post(url).status_code == 302
    assert HelpfulVote.objects.count() == 0
    # Logged in: one POST, one row; voting AGAIN is an idempotent no-op.
    assert client_auth.post(url).status_code == 302
    client_auth.post(url)
    assert HelpfulVote.objects.count() == 1
    assert HelpfulVote.objects.get().owner_id == other_user.pk


@pytest.mark.django_db
def test_answer_vote_endpoint_flow(client_auth, fresh_user, other_user):
    question = qa_services.ask_question(
        author=other_user, title_en="Cheapest data bundle?"
    )
    answer = qa_services.post_answer(
        question=question, author=other_user, body="One GB goes for KSh one hundred."
    )
    url = reverse("trust:answer_vote", args=[answer.pk])
    assert client_auth.post(url).status_code == 302
    assert HelpfulVote.objects.count() == 1
    assert HelpfulVote.objects.get().category == services.GENERAL_CATEGORY
    client_auth.post(url, {"undo": "1"})
    assert HelpfulVote.objects.count() == 0


# ------------------------------------------------------------- rollup math


@pytest.mark.django_db
def test_rollup_math_lifetime_and_last_90_days(
    fresh_user, other_user, third_user
):
    # fresh_user votes helpful on three posts by a mix of OTHER owners.
    for index in range(2):
        _voted_post(other_user, fresh_user)
    _voted_post(third_user, fresh_user)
    # Push the first two votes out of the 90-day window with the test clock…
    old = list(HelpfulVote.objects.filter(owner=other_user).order_by("pk")[:2])
    HelpfulVote.objects.filter(pk__in=[v.pk for v in old]).update(
        created_at=NOW - timedelta(days=100)
    )
    rollup = services.rollup_for(other_user, now=NOW)
    assert rollup == {"GENERAL": {"lifetime": 2, "last90": 0}}
    assert services.rollup_for(third_user, now=NOW) == {
        "GENERAL": {"lifetime": 1, "last90": 1}
    }
    assert list(services.rollup_for(other_user, now=NOW)) == ["GENERAL"]  # sorted keys


@pytest.mark.django_db
def test_rollup_is_empty_and_shape_stable_without_votes(fresh_user):
    assert services.rollup_for(fresh_user) == {}
    assert services.rollup_for(AnonymousUser()) == {}


@pytest.mark.django_db
def test_categories_come_from_the_target(fresh_user, other_user, third_user):
    _voted_post(other_user, fresh_user)
    question = qa_services.ask_question(
        author=other_user,
        title_en="How do I wire a plug safely?",
        safety_category="MEDICAL",
    )
    answer = qa_services.post_answer(
        question=question, author=other_user,
        body="Switch the main off first.",
        safety_warning="Turn the mains off before touching wires.",
    )
    services.record_vote(voter=fresh_user, target=answer)
    rollup = services.rollup_for(other_user)
    assert rollup == {
        "GENERAL": {"lifetime": 1, "last90": 1},
        "MEDICAL": {"lifetime": 1, "last90": 1},
    }
    assert list(rollup) == sorted(rollup)  # deterministic order
    assert services.category_of(answer) == "MEDICAL"  # snapshot, mapped at vote time


# ----------------------------------------------------- threshold crossings


@pytest.mark.django_db
def test_threshold_crossing_emits_badge_event_with_injected_clock(
    fresh_user, other_user, third_user
):
    rising = TrustTier.objects.get(category="GENERAL", level="RISING")
    posts = [
        posts_services.create_post(author=other_user, text=f"Tip {i}.")
        for i in range(rising.min_lifetime)
    ]
    for post in posts[:-1]:  # just under the lifetime threshold …
        services.record_vote(voter=fresh_user, target=post, now=NOW)
    assert not BadgeEvent.objects.exists()
    services.record_vote(voter=third_user, target=posts[-1], now=NOW)  # … the crossing vote
    event = BadgeEvent.objects.get(user=other_user, category="GENERAL", level="RISING")
    assert event.awarded_at == NOW  # evaluated with the injected clock


@pytest.mark.django_db
def test_old_votes_only_earn_when_recency_is_met(
    fresh_user, other_user, third_user
):
    rising = TrustTier.objects.get(category="GENERAL", level="RISING")
    for _ in range(rising.min_lifetime):
        _voted_post(other_user, fresh_user)
    # Everything now sits outside the window: lifetime met, last90 not.
    HelpfulVote.objects.filter(owner=other_user).update(
        created_at=NOW - timedelta(days=120)
    )
    assert services.refresh_badges(other_user, now=NOW) == []
    for _ in range(rising.min_90d):  # fresh recent votes complete the picture
        _voted_post(other_user, third_user)
    assert (
        BadgeEvent.objects.filter(user=other_user, level="RISING").count() == 1
    )


@pytest.mark.django_db
def test_badges_do_not_refire_after_revocation(fresh_user, other_user, third_user):
    known = TrustTier.objects.get(category="GENERAL", level="KNOWN")
    posts = [
        posts_services.create_post(author=other_user, text=f"Tip {i}.")
        for i in range(known.min_lifetime)
    ]
    for post in posts:
        services.record_vote(voter=fresh_user, target=post, now=NOW)
    original = BadgeEvent.objects.get(user=other_user, level="KNOWN")
    for post in posts:  # revoke everything …
        services.remove_vote(voter=fresh_user, target=post)
    for post in posts:  # … then re-cross the SAME threshold
        services.record_vote(voter=fresh_user, target=post, now=NOW)
    # UNIQUE (user, category, level): KNOWN stays one row, original award
    # stamp untouched — and RISING (earned before KNOWN) also stays as-is.
    levels = sorted(
        BadgeEvent.objects.filter(user=other_user).values_list("level", flat=True)
    )
    assert levels == ["KNOWN", "RISING"]
    BadgeEvent.objects.get(pk=original.pk, awarded_at=original.awarded_at)


@pytest.mark.django_db
def test_tie_break_determinism_with_identical_thresholds(
    fresh_user, other_user
):
    # An admin could tune two tiers to the SAME numbers (the structure is
    # admin-tunable — tune EXISTING rows via update, they are UNIQUE per
    # level) — the crossing is then a tie that MUST resolve
    # deterministically. The oracle below replays the vote stream ordered by
    # (created_at, pk), per the services contract.
    known = TrustTier.objects.get(category="GENERAL", level="KNOWN")
    TrustTier.objects.filter(
        category="GENERAL", level="TRUSTED"
    ).update(
        min_lifetime=known.min_lifetime, min_90d=known.min_90d
    )

    posts = [
        posts_services.create_post(author=other_user, text=f"Determinism tip {i}.")
        for i in range(known.min_lifetime)
    ]
    all_votes_same_moment = NOW  # every vote at one instant → pure pk tie-break
    for post in posts:
        fresh_user_id = fresh_user.pk
        services.record_vote(
            voter=get_user_model().objects.get(pk=fresh_user_id),
            target=post,
            now=all_votes_same_moment,
        )

    known_event = BadgeEvent.objects.get(user=other_user, level="KNOWN")
    trusted_event = BadgeEvent.objects.get(user=other_user, level="TRUSTED")
    assert known_event.pk < trusted_event.pk  # deterministic evaluation order
    assert known_event.awarded_at == trusted_event.awarded_at == NOW

    # ORACLE: replay (created_at, pk) and find each tier's FIRST crossing —
    # KNOWN and TRUSTED must cross on the same vote, RISING earlier or same.
    lifetime = 0
    crossing: dict[str, int] = {}
    for index, vote in enumerate(
        HelpfulVote.objects.filter(owner=other_user).order_by("created_at", "pk"),
        start=1,
    ):
        lifetime += 1
        for tier in TrustTier.objects.filter(category="GENERAL"):
            if lifetime >= tier.min_lifetime and lifetime >= tier.min_90d:
                crossing.setdefault(tier.level, index)
    assert crossing["TRUSTED"] == crossing["KNOWN"]
    assert crossing["RISING"] <= crossing["KNOWN"]


# ----------------------------------------------------- consumption surface


@pytest.mark.django_db
def test_usefulness_scorer_is_deterministic_monotone_and_capped(
    fresh_user, other_user
):
    assert services.usefulness_scorer(other_user, "GENERAL") == 0.0
    scores = []
    steps = int(services.SCORER_LIFETIME_NORM) + 2  # run past the cap
    for _ in range(steps):
        _voted_post(other_user, fresh_user)
        scores.append(services.usefulness_scorer(other_user, "GENERAL", now=NOW))
    assert all(0.0 < value <= 1.0 for value in scores)
    assert scores[0] < scores[steps // 2]  # monotone in the rollup
    assert scores[-1] == 1.0  # saturated, not over-promised
    assert scores[-1] == services.usefulness_scorer(other_user, "GENERAL", now=NOW)
    # Voter vs owner: the VOTER's score stays 0.0 for the same category.
    assert services.usefulness_scorer(fresh_user, "GENERAL", now=NOW) == 0.0


@pytest.mark.django_db
def test_helpfulness_badges_show_the_top_held_tier_per_category(
    fresh_user, other_user, third_user
):
    rising = TrustTier.objects.get(category="GENERAL", level="RISING")
    known = TrustTier.objects.get(category="GENERAL", level="KNOWN")
    posts = [
        posts_services.create_post(author=other_user, text=f"Tip {i}.")
        for i in range(known.min_lifetime)
    ]
    for post in posts[: rising.min_lifetime]:
        services.record_vote(voter=fresh_user, target=post, now=NOW)
    assert [badge["level"] for badge in services.helpfulness_badges(other_user)] == [
        "RISING"
    ]
    for post in posts[rising.min_lifetime :]:
        services.record_vote(voter=third_user, target=post, now=NOW)
    badges = services.helpfulness_badges(other_user)
    assert len(badges) == 1  # one entry per category — TOP tier only
    assert badges[0]["level"] == "KNOWN"
    assert badges[0]["category"] == "GENERAL"
    assert badges[0]["category_label"] and badges[0]["label"]
    assert badges[0]["awarded_at"] == NOW
    assert services.helpfulness_badges(fresh_user) == []  # voter earned nothing


@pytest.mark.django_db
def test_badges_partial_renders_and_stays_off_empty_pages(
    fresh_user, other_user
):
    empty = render_to_string("trust/_badges.html", {"trust_badges": []})
    assert "trust-badges" not in empty
    tier = TrustTier.objects.get(category="GENERAL", level="RISING")
    for _ in range(tier.min_lifetime):
        _voted_post(other_user, fresh_user)
    html = render_to_string(
        "trust/_badges.html",
        {"trust_badges": services.helpfulness_badges(other_user)},
    )
    assert str(TrustLevel.RISING.label) in html  # gettexted label, server-side
    assert_under_budget(html, 50)  # the partial itself is tiny by budget rule
