"""Prompt 11 acceptance: the utility feed ranks with a PURE, injected-clock
rank() whose ordering is fully explainable from the scoring table in
apps/feed/ranking.py's docstring; serves pages of exactly 10 with an
explicit end-of-feed card (no bottomless scroll); recency only ever breaks
ties WITHIN a tier; and the REMOVED/blocked exclusion goes through the
public helpers (posts visible_posts + moderation excluding_blocked_for).

The six hand-built ranking scenarios (pure, no DB — rows are plain dicts:
rank() is a pure function so tests build rows by hand, exactly as Prompt
16's trust scorer will be plugged in later):
  1. local_useful_beats_recent_unrelated
  2. followed_beats_stranger_at_equal_usefulness
  3. same_ward_beats_ward_outsider_at_equal_age
  4. injected_clock_determinism (same result at two different `now`s)
  5. usefulness_scorer_plugin_overrides_default_zero (mock-first contract)
  6. group_overlap_signal_reserved_but_wired
"""
from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.contrib.auth import get_user_model

from apps.core.testing import assert_under_budget
from apps.feed import services
from apps.feed.ranking import rank, zero_usefulness
from apps.moderation.services import block_user
from apps.posts.models import Post
from apps.posts.services import create_post, remove_post

UTC = dt_tz.utc
NOW = datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)  # mirrors core.testing.mock_now style
YESTERDAY = NOW - timedelta(days=1)
MINUTES_AGO = NOW - timedelta(minutes=5)


# --- hand-built row/context builders (rank() is pure — no DB needed) --------

def row(pk: int, author_id: int, created_at: datetime = MINUTES_AGO, **flags) -> dict:
    """One hand-built row matching the dict shape services.assemble_rows
    feeds into rank(). Defaults: a plain, unrelated, unlinked post."""
    return {
        "pk": pk,
        "author_id": author_id,
        "author_ward": flags.get("ward", ""),
        "created_at": created_at,
        "educational": flags.get("educational", False),
        "ref_guide_id": 1 if flags.get("guide_linked") else None,
        "ref_listing": None,
        "author_group_ids": flags.get("author_group_ids", []),
    }


def ctx(ward: str = "", followed=(), groups=()) -> dict:
    return {
        "viewer_ward": ward,
        "followed_user_ids": list(followed),
        "viewer_group_ids": list(groups),
    }


# --- the six ranking scenarios ----------------------------------------------

def test_local_useful_beats_recent_unrelated():
    """SCENARIO 1: a same-ward, guide-linked post from YESTERDAY outranks a
    plain post written 5 minutes ago — freshness must never outrank fit."""
    useful_old = row(101, author_id=9, created_at=YESTERDAY, ward="Kilimani", guide_linked=True)
    fresh_unrelated = row(102, author_id=8, created_at=MINUTES_AGO, ward="Umoja")
    ranked = rank([fresh_unrelated, useful_old], context=ctx(ward="Kilimani"), now=NOW)
    assert [r["pk"] for r in ranked] == [101, 102]
    # Exact scorecard — every point traceable to the docstring table:
    assert ranked[0]["breakdown"] == {
        "followed": 0.0, "local": 2.0, "usefulness": 0.0,
        "linked": 1.0, "educational": 0.0, "group": 0.0,
    }
    assert ranked[0]["score"] == 3.0 and ranked[1]["score"] == 0.0


def test_followed_beats_stranger_at_equal_usefulness():
    """SCENARIO 2: two otherwise identical posts created in the SAME instant
    — the followed author's wins purely on the followed tier (+2), not on
    recency (timestamps are equal)."""
    followed_post = row(201, author_id=2, created_at=MINUTES_AGO)
    stranger_post = row(202, author_id=1, created_at=MINUTES_AGO)
    ranked = rank(
        [stranger_post, followed_post],
        context=ctx(followed=[2]),
        now=NOW,
    )
    assert [r["pk"] for r in ranked] == [201, 202]
    assert ranked[0]["breakdown"]["followed"] == 2.0
    assert ranked[1]["breakdown"]["followed"] == 0.0
    # Equal usefulness everywhere (default scorer is the honest constant 0).
    assert ranked[0]["breakdown"]["usefulness"] == 0.0


def test_same_ward_beats_ward_outsider_at_equal_age():
    """SCENARIO 3: the local tier in isolation — nothing else differs except
    the author's ward; the in-ward post wins even written a day earlier
    (same-ward +2 beats any freshness advantage)."""
    local_old = row(301, author_id=7, created_at=NOW - timedelta(days=1), ward="Dandora")
    outsider_fresh = row(302, author_id=6, created_at=MINUTES_AGO, ward="Umoja")
    ranked = rank([outsider_fresh, local_old], context=ctx(ward="Dandora"), now=NOW)
    assert [r["pk"] for r in ranked] == [301, 302]
    assert ranked[0]["breakdown"]["local"] == 2.0
    # Case-insensitive ward labels agree (' kilimani ' == 'Kilimani'):
    ranked_ci = rank(
        [row(303, author_id=7, ward=" kilimani ")],
        context=ctx(ward="Kilimani"),
        now=NOW,
    )
    assert ranked_ci[0]["breakdown"]["local"] == 2.0


def test_injected_clock_determinism():
    """SCENARIO 4: two injected clocks a month apart produce the IDENTICAL
    ordering and scores — and an exact score tie resolves by created_at
    (recency WITHIN the tier, never as the tier itself)."""
    tie_old = row(401, author_id=5, created_at=NOW - timedelta(hours=2))
    tie_new = row(402, author_id=4, created_at=MINUTES_AGO)
    first = rank([tie_old, tie_new], context=ctx(), now=NOW)
    later_clock = rank(
        [tie_old, tie_new], context=ctx(), now=NOW + timedelta(days=30)
    )
    assert first == later_clock  # same rows, same order, same scorecards
    assert [r["pk"] for r in first] == [402, 401]  # tie → fresher within tier
    # And the ordering is NOT the clock's doing: with a newer timestamp the
    # tie flips, proving the tie-break is row time, not the injected now.
    flipped = rank(
        [
            row(403, author_id=5, created_at=MINUTES_AGO),
            row(404, author_id=4, created_at=NOW - timedelta(hours=2)),
        ],
        context=ctx(),
        now=NOW,
    )
    assert [r["pk"] for r in flipped] == [403, 404]  # input order never wins — row time does
    assert "recency" not in first[0]["breakdown"]  # recency carries NO weight ever

def test_usefulness_scorer_plugin_overrides_default_zero():
    """SCENARIO 5 (mock-first contract): without Prompt 16's trust scorer the
    usefulness tier is exactly 0.0 for everyone; a PLUGGED scorer returning
    1.0 for one row earns that row +2.0 and it wins — via injection only,
    with no import of the sibling trust app."""
    plain_fresh = row(501, author_id=3, created_at=MINUTES_AGO)
    plain_old = row(502, author_id=9, created_at=YESTERDAY)
    default_ranked = rank([plain_old, plain_fresh], context=ctx(), now=NOW)
    assert [r["pk"] for r in default_ranked] == [501, 502]  # fresher first in a 0-0 tier
    assert all(r["breakdown"]["usefulness"] == 0.0 for r in default_ranked)

    def trust_scorer_like_mock(r):
        # Shape a real helpful-vote-ratio scorer already has: row in → [0..1] out.
        return 1.0 if r["pk"] == 502 else 0.0

    plugged = rank(
        [plain_old, plain_fresh],
        context=ctx(),
        now=NOW,
        usefulness_scorer=trust_scorer_like_mock,
    )
    assert [r["pk"] for r in plugged] == [502, 501]  # useful yesterday beats fresh plain
    assert plugged[0]["breakdown"]["usefulness"] == 2.0  # 1.0 × USEFULNESS_SCALE


def test_group_overlap_signal_reserved_but_wired():
    """SCENARIO 6: the Prompt 13 integration point (member_group_ids) is
    consumed by rank() today — a row carrying author_group_ids overlapping
    the viewer's groups earns +1 — while posts without group scoping score 0
    (group_tier_posts is an honest [] until posts grow a group FK)."""
    same_group = row(601, author_id=2, author_group_ids=[7], created_at=YESTERDAY)
    no_group = row(602, author_id=3, created_at=MINUTES_AGO)
    ranked = rank([no_group, same_group], context=ctx(groups=[7]), now=NOW)
    assert ranked[0]["breakdown"]["group"] == 1.0
    assert ranked[1]["breakdown"]["group"] == 0.0


# --- view-level tests: pagination, honesty filters, budget -------------------

def _make_user(name: str):
    User = get_user_model()
    return User.objects.create_user(username=name, email=f"{name}@example.com", password="***")


def _backdate(post: Post, created_at: datetime) -> None:
    # auto_now_add owns INSERT time; the queryset update is the honest way to
    # place a seeded post at a chosen point on the timeline.
    Post.objects.filter(pk=post.pk).update(created_at=created_at)


@pytest.mark.django_db
def test_feed_pages_of_ten_with_end_of_feed_marker():
    """25 posts → exactly 3 pages of 10/10/5; the muted end-of-feed card
    appears ONLY on the last page; a stray ?page= lands gracefully."""
    viewer = _make_user("pager")
    for i in range(25):
        _backdate(
            create_post(author=viewer, text=f"Useful tip number {i:02d} about matatu fares."),
            NOW - timedelta(minutes=i),
        )
    from django.test import Client

    client = Client()
    page1 = client.get("/feed/")
    assert page1.status_code == 200 and len(page1.context["items"]) == 10
    assert page1.context["page_obj"].has_next()
    assert b"That's everything for now" not in page1.content

    page2 = client.get("/feed/?page=2")
    assert len(page2.context["items"]) == 10 and page2.context["page_obj"].has_next()

    page3 = client.get("/feed/?page=3")
    assert len(page3.context["items"]) == 5  # the explicit partial last page
    assert not page3.context["page_obj"].has_next()
    assert page3.context["end_of_feed"]
    assert b"That's everything for now" in page3.content
    assert b"This feed is finite on purpose" in page3.content

    stray = client.get("/feed/?page=99")  # no 500, no bottomless pit — last page
    assert stray.status_code == 200 and stray.context["end_of_feed"]


@pytest.mark.django_db
def test_removed_and_blocked_posts_excluded_through_public_helpers():
    """REMOVED leaves via posts' visible_posts; a BLOCKED author's posts
    leave via moderation's excluding_blocked_for — per-viewer only."""
    viewer = _make_user("viewer")
    followed = _make_user("followed")
    blocked = _make_user("blockedx")
    removed = _make_user("removedx")
    outsider = _make_user("outsider")
    for user in (viewer, followed, blocked, removed):
        from apps.profiles.services import get_profile
        get_profile(user)  # viewer_context reads wards lazily via profiles' public fn

    p_followed = create_post(author=followed, text="Followed author shares a changaa route tip.")
    p_blocked = create_post(author=blocked, text="Blocked author sells 'get rich quick kits'.")
    p_removed = create_post(author=removed, text="This post is being soft-removed from feeds.")
    remove_post(p_removed, by_user=removed)  # posts' PUBLIC write door → REMOVED
    block_user(blocker=viewer, blocked=blocked)  # moderation's PUBLIC write door

    from django.test import Client

    client = Client()
    client.force_login(viewer)
    response = client.get("/feed/")
    html = response.content.decode()
    assert response.status_code == 200
    assert p_followed.text in html  # tier feed keeps the followed author
    assert p_blocked.text not in html  # blocked-for-this-viewer only
    assert p_removed.text not in html  # REMOVED everywhere
    # A DIFFERENT viewer still sees the blocked author's post (block ≠ ban);
    # nobody sees the REMOVED post (site-wide status).
    client_out = Client()
    client_out.force_login(outsider)
    response_out = client_out.get("/feed/")
    # WHY unescape: post text renders autoescaped (' → &#x27;); membership
    # assertions compare against the DOM's decoded text, not raw HTML.
    from html import unescape
    html_out = unescape(response_out.content.decode())
    assert p_blocked.text in html_out
    assert p_removed.text not in html_out


@pytest.mark.django_db
def test_feed_html_under_budget_with_ten_posts():
    """Budget hard rule: authenticated feed page ≤ 50 KB — asserted through
    apps.core.testing.assert_under_budget with the real pipeline's output."""
    viewer = _make_user("buddget")
    filler = "Boda fare Kikuyu gate kwa sasa ni shilingi mia moja usiku wa manane. " * 3
    for i in range(10):
        create_post(author=viewer, text=f"{filler}(seed {i})")
    from django.test import Client

    client = Client()
    client.force_login(viewer)
    response = client.get("/feed/")
    assert_under_budget(response.content.decode(), kb=50)


@pytest.mark.django_db
def test_anonymous_feed_public_posts_and_end_state():
    """Anonymous feed: public posts only, no followed/local tier facts are
    even fetched (wards/follows degrade to []), single page still carries
    the explicit end-of-feed card."""
    author = _make_user("anon_seed")
    create_post(author=author, text="Public market price check: sukuma 10/bunch.")
    from django.test import Client

    response = Client().get("/feed/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "sukuma" in html and b"That's everything for now" in response.content
    assert response.context["end_of_feed"]  # marker even without pagination widget


# --- services-level guardrails -----------------------------------------------

@pytest.mark.django_db
def test_assemble_rows_sorts_by_score_not_pure_recency():
    """Assembly-level proof of the anti-bait goal end to end over REAL rows:
    a ward-matching educational post from yesterday must finish above a
    fresh plain post, using the same public helpers the view uses."""
    viewer = _make_user("asm_viewer")
    local = _make_user("asm_local")
    fresh = _make_user("asm_fresh")
    from apps.profiles.services import get_profile
    viewer_ward = "Dandora"
    # NOTE: get_profile returns the Profile — save the PROFILE, not the user.
    profile_viewer = get_profile(viewer)
    profile_viewer.ward = viewer_ward  # the boost compares VIEWER ward…
    profile_viewer.save()
    profile_local = get_profile(local)
    profile_local.ward = viewer_ward   # …against AUTHOR ward
    profile_local.save()
    local_post = create_post(
        author=local, text="Local electrician numbers around Dandora market.",
        educational=True,
    )
    fresh_post = create_post(author=fresh, text="Just rage-baiting, 5 minutes old.")
    _backdate(local_post, NOW - timedelta(days=1))
    _backdate(fresh_post, MINUTES_AGO)
    ranked = services.assemble_rows(viewer, now=NOW, usefulness_scorer=None)
    assert [r["pk"] for r in ranked] == [local_post.pk, fresh_post.pk]
    assert ranked[0]["score"] == 3.0  # local 2 + educational 1, usefulness 0 default
    assert ranked[0]["breakdown"]["usefulness"] == 0.0
    assert zero_usefulness(ranked[0]) == 0.0
