"""Feed assembly — a READ-ONLY view over the other apps' PUBLIC functions.

PROMPTS.md Prompt 11: "Feed assembly is a read-only view over
posts/follows — no denormalized state to drift." This module therefore
holds ZERO models, ZERO cache tables and ZERO writes of its own; it exists
to (1) gather the viewer's tier facts through one public call per app, and
(2) shape Post rows (as returned by posts' PUBLIC read API) into the plain
dicts that ranking.rank() consumes. NOTE: posts rows are the Post OBJECTS
the public function returns — we read attributes off them, we never import
posts/models or friends' internal tables (one-agent-per-file hard rule).

The tier facts, and the one public call each (single calls forever — the
PROMPTS 13 acceptance contract):
    followed_user_ids(viewer)   apps.follows.services
    member_group_ids(viewer)    apps.groups.services
    profile ward (viewer+authors) apps.profiles.services.get_profile
    visible_posts()             apps.posts.services (PUBLISHED only →
                                REMOVED never reaches the feed)
    excluding_blocked_for()     apps.moderation.services (wave-1 public
                                helper: hides blocked authors per viewer)
"""
from __future__ import annotations

from collections.abc import Iterable

from apps.follows.services import followed_user_ids
from apps.groups.services import member_group_ids
from apps.moderation.services import excluding_blocked_for
from apps.posts.services import visible_posts
from apps.profiles.services import get_profile

from .ranking import rank, zero_usefulness

# WHY 10 exactly: PROMPTS.md — "NO infinite-scroll bottomless pit:
# paginated pages of 10 with an explicit 'end of feed' state."
PAGE_SIZE = 10


def make_trust_scorer():
    """Adapter factory: one scorer per feed build (Prompt 16 plug-in).

    Parent wiring: Prompt 11 shipped with the constant-0 scorer (mock-first);
    Prompt 16's usefulness_scorer(user, category) is the real signal. Posts
    carry no category of their own → the GENERAL rollup is the honest input.
    Returns a CLOSURE with a build-local memo keyed by author id, so a page
    build costs one rollup per DISTINCT author, not per row — and nothing
    is stored on rows themselves (rank's rows stay plain dicts).
    """
    from apps.trust.services import usefulness_scorer as trust_scorer

    scores: dict[int, float] = {}

    def score(row: dict) -> float:
        author_id = row["author_id"]
        if author_id not in scores:
            scores[author_id] = trust_scorer(row["author"], "GENERAL")
        return scores[author_id]

    return score


def viewer_context(viewer) -> dict:
    """The viewer's tier facts, gathered PUBLICLY (one call per app)."""
    authenticated = getattr(viewer, "is_authenticated", False)
    return {
        "followed_user_ids": followed_user_ids(viewer),
        # member_group_ids is the Prompt 13 "one function call" integration —
        # consumed by the group-overlap signal once posts grow group scoping.
        "viewer_group_ids": member_group_ids(viewer) if authenticated else [],
        # Local tier: the viewer's own profile ward (plain label, no coords).
        # Anonymous feeds have no ward ("local tier uses no ward") → "" is
        # exactly what rank() needs to grant no boost.
        "viewer_ward": get_profile(viewer).ward if authenticated else "",
    }


def _author_wards(posts: Iterable) -> dict[int, str]:
    """Ward per author, deduped by author id.

    WHY not one big query: profiles exposes get_profile() as its one public
    accessor (lazy get_or_create), so a batched reader does not exist yet —
    we dedupe authors to keep the calls minimal and leave the batched
    accessor as profiles' future improvement, not ours to build here.
    """
    wards: dict[int, str] = {}
    for post in posts:
        aid = post.author_id
        if aid not in wards:
            wards[aid] = get_profile(post.author).ward
    return wards


def assemble_rows(viewer, *, usefulness_scorer=None, now) -> list[dict]:
    """The full ranked feed for `viewer`: read-only assembly + one pure
    rank pass. `now` is injected from the view (the real clock is allowed
    THERE — only rank() itself must not read it).

    `usefulness_scorer=None` (the view's explicit sentinel) selects the
    default constant-0 scorer — the view never silently picks its own.

    Order of operations is the privacy story: posts' visible_posts() has
    already dropped DRAFT/UNDER_REVIEW/REMOVED; moderation's PUBLIC
    excluding_blocked_for() then drops authors THIS viewer blocked — so a
    blocked member's posts vanish for the blocker only, exactly the wave-1
    contract. No REMOVED or blocked row is ever scored or paginated.
    """
    posts = excluding_blocked_for(
        viewer, visible_posts(), author_field="author"
    ).select_related("author")
    posts = list(posts)  # one pass; ward lookup reuses the list
    wards = _author_wards(posts)
    context = viewer_context(viewer)
    scorer = usefulness_scorer if usefulness_scorer is not None else zero_usefulness
    rows = [
        {
            "pk": post.pk,
            "author_id": post.author_id,
            # NOTE: kept for the template's author line; ward VALUES are
            # never rendered to other members (KDPA coarse-location rule).
            "author": post.author,
            "author_ward": wards.get(post.author_id, ""),
            "created_at": post.created_at,
            "educational": post.educational,
            "ref_guide_id": post.ref_guide_id,
            "ref_listing": post.ref_listing,
            # The Post instance itself — the template renders from it; rank()
            # only copies dicts around (it never dereferences the object).
            "post": post,
        }
        for post in posts
    ]
    return rank(
        rows,
        context=context,
        now=now,
        usefulness_scorer=scorer,
    )
