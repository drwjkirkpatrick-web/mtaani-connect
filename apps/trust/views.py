"""Trust views (Prompt 16) — POST-only vote endpoints, login required.

There are no read pages of trust's own on purpose (mobile budget + "trust is
served where content lives"): badges for profiles/answers come from
services.helpfulness_badges via the _badges.html partial that other templates
{% include %}; ranking reads services.usefulness_scorer.

Each endpoint is IDEMPOTENT per (user, target): POST creates-or-keeps one
vote (voting again never stacks), POST undo=1 revokes. Targets are fetched
through the OTHER app's public read-door only (posts.get_visible_post /
qa.visible_answers) — REMOVED content 404s before the service is reached,
and the service re-checks the not-REMOVED rule for every future caller.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.posts import services as posts_services
from apps.qa import services as qa_services

from . import services


def _safe_next(request) -> str | None:
    """A local redirect target from POST (no open redirects, no //host)."""
    candidate = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return None


def _vote_action(request, target) -> None:
    """Run the vote service for one target and set the message only (no
    response) so each endpoint can redirect to ITS own content URL."""
    undo = bool(request.POST.get("undo"))
    try:
        if undo:
            if services.remove_vote(voter=request.user, target=target):
                messages.success(request, _("Helpful vote removed."))
        else:
            services.record_vote(voter=request.user, target=target)
            messages.success(request, _("Marked as helpful — thank you."))
    except ValidationError as exc:
        messages.error(request, " ".join(getattr(exc, "messages", None) or [str(exc)]))


def _respond(request, fall_back_to: str) -> HttpResponseRedirect:
    """Redirect to a validated ?next= when present, else the content page."""
    return HttpResponseRedirect(_safe_next(request) or fall_back_to)


@login_required
@require_POST
def post_vote(request, post_id):
    """POST /trust/posts/<id>/vote/ — helpful vote on a post (login, POST,
    idempotent; POST undo=1 revokes)."""
    post = posts_services.get_visible_post(post_id, request.user)
    if post is None:
        raise Http404  # REMOVED/draft + not yours: the posts read-door decides
    _vote_action(request, post)
    return _respond(request, reverse("posts:detail", args=[post.pk]))


@login_required
@require_POST
def answer_vote(request, answer_id):
    """POST /trust/answers/<id>/vote/ — helpful vote on a community answer."""
    # qa's public read-door: answers under REMOVED questions are absent here,
    # so the 404 below is the visibility decision (same as qa's own vote view).
    answer = qa_services.visible_answers(request.user).filter(pk=answer_id).first()
    if answer is None:
        raise Http404
    _vote_action(request, answer)
    return _respond(request, reverse("qa:detail", args=[answer.question_id]))
