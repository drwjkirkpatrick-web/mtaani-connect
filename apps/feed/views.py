"""Feed view (Prompt 11) — one GET, pages of 10, an explicit end-of-feed
state, and NO JavaScript (mobile-first: the pager is links, like posts').

The only clock usage in the whole app lives HERE: the view calls
timezone.now() once and INJECTS it into the pure rank() — never the other
way round (PROMPTS.md pitfall: now() inside ranking is untestable).
"""
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils import timezone

from . import services


def feed(request):
    """The utility feed. Anonymous visitors get the same page: public
    posts, no followed tier, no local tier (they have no ward) — ranking
    is still the same pure function with an empty context."""
    ranked = services.assemble_rows(
        request.user,
        # Parent wiring, Prompt 16 landed: the real usefulness signal is
        # trust's public scorer via services.make_trust_scorer() (one rollup
        # per distinct author per page build).
        usefulness_scorer=services.make_trust_scorer(),
        now=timezone.now(),
    )
    paginator = Paginator(ranked, services.PAGE_SIZE)
    # get_page() is the stray-querystring safety net: a bad ?page= lands on
    # page 1 instead of a 500 (same posture as the posts list view).
    page_obj = paginator.get_page(request.GET.get("page"))
    # WHY an explicit end state (not silent nothing): PROMPTS.md — no
    # bottomless pit; when there is no next page the page NAMES the end.
    # NOTE: Django ≥6 made Page.has_next a METHOD (it was a property) —
    # `not page_obj.has_next` silently saw a truthy bound method and the
    # end card never fired; the template's {% if %} auto-calls callables,
    # which is exactly why only this Python line needed the ().
    end_of_feed = ranked and not page_obj.has_next()
    return render(
        request,
        "feed/list.html",
        {
            "items": page_obj.object_list,
            "page_obj": page_obj,
            "end_of_feed": end_of_feed,
            "total_count": paginator.count,
        },
    )
