"""Follow views (PROMPTS.md Prompt 13).

Read pages (following / followers lists) are public — the social graph is a
soft signal — but any follower count they display MUST come from
services.visible_follower_count (below-10 privacy floor). Write actions are
login_required and POST-only (state changes are never GETs).
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.profiles.services import get_profile, public_data

from . import services

# HARD RULE (PROMPTS.md budgets): list surfaces show at most 20 items per
# page — keeps every page inside the 50 KB mobile budget.
PAGE_SIZE = 20


def _target(user_id):
    return get_object_or_404(get_user_model(), pk=user_id)


def _safe_next(request):
    """A local 'next' path only — never an open redirect ('//evil' excluded)."""
    next_url = request.POST.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return None


def _list(request, user_id, *, mode):
    """One renderer for both list pages (same shape, different direction)."""
    target = _target(user_id)
    rows = (
        services.followers_of(target)
        if mode == "followers"
        else services.following_of(target)
    )
    page_obj = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = list(page_obj.object_list)

    # Row-level button states: annotate Follow rows (never leak model
    # internals into templates — these are display flags only).
    if mode == "following":
        for row in rows:
            row.viewer_can_unfollow = (
                request.user.is_authenticated and request.user.pk == target.pk
            )
    else:
        for row in rows:
            row.viewer_can_follow = (
                request.user.is_authenticated
                and request.user.pk != row.follower_id
                and not services.is_following(
                    follower=request.user, followee=row.follower
                )
            )

    # The ONLY follower count on the page — None below 10 (privacy floor).
    follower_count = services.visible_follower_count(target)
    profile = get_profile(target)
    header = public_data(profile, viewer_is_member=request.user.is_authenticated)
    return render(
        request,
        "follows/following_list.html" if mode == "following" else "follows/followers_list.html",
        {
            "target": target,
            "header": header,
            "rows": rows,
            "page_obj": page_obj,
            "follower_count": follower_count,
            "back_url": request.get_full_path(),
        },
    )


def following_list(request, user_id):
    return _list(request, user_id, mode="following")


def followers_list(request, user_id):
    return _list(request, user_id, mode="followers")


@login_required
@require_POST
def follow_action(request, user_id):
    """Follow target (idempotent). Redirection honours a local `next`."""
    target = _target(user_id)
    try:
        services.follow(follower=request.user, followee=target)
    except services.SelfFollow as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, _("You now follow %(username)s.") % {"username": target.username}
        )
    return redirect(_safe_next(request) or _target_profile_url(request, target))


@login_required
@require_POST
def unfollow_action(request, user_id):
    """Unfollow target (idempotent). Unfollowing a non-follow: no error."""
    target = _target(user_id)
    if services.unfollow(follower=request.user, followee=target):
        messages.success(
            request, _("Unfollowed %(username)s.") % {"username": target.username}
        )
    return redirect(_safe_next(request) or _target_profile_url(request, target))


def _target_profile_url(request, target) -> str:
    """Landing spot after a follow action: the target's public profile
    (which is where every follow button ultimately lives)."""
    from django.urls import reverse

    _ = request  # silence unused; signature kept uniform for future options
    return reverse("profiles:profile", args=[target.username])
