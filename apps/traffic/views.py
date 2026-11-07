"""Views for the traffic app (Prompt 32) — orchestration ONLY.

The arithmetic lives in apps.traffic.graph (pure); these views resolve
stage names, fetch rows through each app's public doors (see services.py
docstring), call the planner once per request and render the payload +
its JSON mirror. ALL honesty labels ride inside the payload (sources per
leg, cited community conditions, observed fare ranges) so neither page
can render a claim without its evidence.

Public surfaces: planning needs no login (it rides on PUBLIC route data
and public crowd reads — the same door discipline as crowd's stage feed).
"""
from __future__ import annotations

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from . import services


def _resolve_queries(raw_from: str, raw_to: str) -> tuple[dict | None, dict | None, dict]:
    """Resolve both side queries through the routes search public door.

    Returns (from_row | None, to_row | None, context) where the context
    carries the raw queries plus each door's ranked suggestions for the
    honest "not recognized" state. Nothing is guessed: an unrecognized
    name resolves to None and the door's hits are shown instead.
    """
    from_row, from_suggest = services.resolve_stage_query(raw_from)
    to_row, to_suggest = services.resolve_stage_query(raw_to)
    context = {
        "raw_from": raw_from,
        "raw_to": raw_to,
        "from_suggestions": from_suggest,
        "to_suggestions": to_suggest,
        "from_row": from_row,
        "to_row": to_row,
    }
    return from_row, to_row, context


def _unrecognized_queries(ctx: dict) -> list[tuple[str, list[dict]]]:
    """(query, suggestions) for each side the door could not resolve —
    the honest per-side miss list the page and mirror render."""
    misses = []
    if ctx["from_row"] is None and ctx["raw_from"]:
        misses.append((ctx["raw_from"], ctx["from_suggestions"]))
    if ctx["to_row"] is None and ctx["raw_to"]:
        misses.append((ctx["raw_to"], ctx["to_suggestions"]))
    return misses


def plan_page(request: HttpRequest) -> HttpResponse:
    """The journey-planning page: GET /traffic/plan/?from=X&to=Y.

    Unknown stage names render the form back with the door's suggested
    matches (an honest miss, never a near-miss plan); a disconnected
    network renders services' honest empty state.
    """
    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    from_row, to_row, ctx = _resolve_queries(raw_from, raw_to)
    context = dict(ctx)
    if from_row and to_row:
        context["payload"] = services.journey_payload(from_row, to_row, now=timezone.now())
    else:
        misses = _unrecognized_queries(ctx)
        if misses and (raw_from or raw_to):
            context["message"] = services.MSG_UNKNOWN % {"query": misses[0][0]}
    return render(request, "traffic/plan.html", context)


def plan_json(request: HttpRequest) -> JsonResponse:
    """The machine mirror of the plan page — SAME payload, same honesty.

    A 200 with status NO_ROUTE / TRIVIAL is a SUCCESSFUL honest answer
    (the URL is right; the world just has no route to show) — 404 stays
    reserved for a wrong URL, not for "the network cannot connect these".
    """
    raw_from = (request.GET.get("from") or "").strip()
    raw_to = (request.GET.get("to") or "").strip()
    from_row, to_row, ctx = _resolve_queries(raw_from, raw_to)
    data = {"from_query": raw_from, "to_query": raw_to}
    if from_row and to_row:
        data["payload"] = services.journey_payload(from_row, to_row, now=timezone.now())
    else:  # at least one side unrecognized — say which, suggest door hits
        misses = _unrecognized_queries(ctx)
        first_miss = misses[0] if misses else ("", [])
        data["payload"] = {
            "status": "UNKNOWN_STAGE",
            "message": services.MSG_UNKNOWN % {"query": first_miss[0]},
            "suggestions": [
                {"id": int(s["id"]), "name": str(s["name"])}
                for _q, rows in misses
                for s in rows[: services.MAX_SUGGESTIONS]
            ],
        }
    return JsonResponse(data)


def about(request: HttpRequest) -> HttpResponse:
    """The honesty note — what the planner can and cannot know.

    Static on purpose: this page states the SOURCE-SEPARATION contract
    (official/seeded route data vs community-reported conditions) that
    the plan page splits visually by design, plus the data-budget and
    "no fabricated data" hard rules in plain words.
    """
    return render(request, "traffic/about.html", {})
