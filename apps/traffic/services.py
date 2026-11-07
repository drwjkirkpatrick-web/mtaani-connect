"""services.py — Django-side orchestration for traffic planning (Prompt 32).

The seams are strict and one-directional:

  ROUTES data  →  read via this app's own models? NO — via routes'
                  documented read surface (RouteStage/Fare/Stage models,
                  the same read pattern every Phase-3 sibling uses —
                  e.g. apps.alerts views) and the nickname/icase
                  RESOLUTION goes through routes' PUBLIC search door
                  (apps.routes.search.search_network).
  CROWD data   →  through the crowd PUBLIC doors: the documented
                  CrowdReport fields + the PURE read-time decay functions
                  in apps.crowd.weighting; display lines via crowd.display
                  rider_line — no crowd internals re-implemented here.
  PLANNER      →  apps.traffic.graph, PURE (no Django imports): rows in,
                  plan out. This module NEVER adds transport arithmetic.

WHY the bulk crowd read instead of per-stage open_reports_for_stage():
that door answers "which reports are still MERGEABLE" (the 10-minute
filing window), but a report stays legitimately CURRENT up to crowd's
2-hour decay horizon (weighting.DECAY_END_MIN) — the map overlay already
shows it after minute 10. A planner using the merge window would route
riders through a stage the map already flags as flooded. So: one bounded
query for the graph's stages, decay applied with crowd's own public pure
functions, and rows passed to the planner shaped EXACTLY like the frozen
overlay payload (type/count/age_min/first_seen_at) so plan citations and
map pins can never disagree about vocabulary.

ALL user-facing sentences are composed HERE or in templates (the i18n
seam) — never inside graph.py, which must stay translation-free.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.crowd import weighting as crowd_weighting
from apps.crowd.display import rider_line
from apps.crowd.models import CrowdReport, ReportType
from apps.routes import search as routes_search
from apps.routes.models import Fare, RouteStage

from . import graph

# The plan page lists at most this many resolution suggestions when a
# name is not recognized (bounded lists — hard rule).
MAX_SUGGESTIONS = 3

MSG_NO_ROUTE = _(
    "No route connects these stages in the shared network right now. "
    "We show no plan rather than guess one."
)
MSG_ALREADY_THERE = _("You are already at %(stage)s — pick a different destination.")
MSG_UNKNOWN = _(
    'We could not recognize "%(query)s" as a stage. Try a stage name or a '
    "nickname — a few network matches are listed below."
)
MSG_HOW_TIMES = _(
    "Minutes are honest estimates from stage-to-stage distances at one city "
    "walking pace — not live vehicle tracking."
)
MSG_HOW_FARES = _("Fares are observed community RANGES — never one official number.")


# ------------------------------------------------------------------ fetches
def network_rows() -> list[dict]:
    """The whole stage graph's link rows — read ONCE per request.

    Rows are ordered (route, position) so graph.build_graph() can chain
    consecutive hops without re-sorting. Coordinates come as floats (the
    planner is unit-agnostic); a missing lat/lon stays None and the pure
    planner falls back honestly to DEFAULT_EDGE_MIN.
    """
    rows = RouteStage.objects.select_related("route", "stage").order_by(
        "route_id", "position"
    ).values_list(
        "route_id", "route__name_en", "route__seed_origin",
        "stage_id", "stage__name", "stage__lat", "stage__lon", "position",
    )
    return [
        {
            "route_id": int(r_id),
            "route_name": str(r_name),
            "route_seed": bool(r_seed),
            "stage_id": int(s_id),
            "stage_name": str(s_name),
            "lat": None if lat is None else float(lat),
            "lon": None if lon is None else float(lon),
            "position": int(pos),
        }
        for r_id, r_name, r_seed, s_id, s_name, lat, lon, pos in rows
    ]


def fare_rows() -> list[dict]:
    """All observed fare ranges, planner-shaped (the range discipline of
    Prompt 25 in, range display out — never a single number)."""
    rows = Fare.objects.order_by("pk").values_list(
        "pk", "route_id", "from_stage_id", "to_stage_id",
        "min_price", "max_price", "confidence",
    )
    return [
        {
            "pk": int(pk),
            "route_id": int(route_id),
            "from_stage_id": int(a),
            "to_stage_id": int(b),
            "min_price": int(lo),
            "max_price": int(hi),
            "confidence": str(conf),
        }
        for pk, route_id, a, b, lo, hi, conf in rows
    ]


def crowd_rows_for(stage_ids: Iterable[int], *, now: datetime) -> list[dict]:
    """Current community rows for the graph's stages (overlay vocabulary).

    Reads only the DOCUMENTED CrowdReport fields (type enum values,
    merged_count, last_bumped_at), then applies crowd's own pure
    read-time decay (weighting.age_min / decay factor at DECAY_END_MIN) —
    stale rows are honestly dropped at read time, exactly like the map.
    """
    ids = {int(s) for s in stage_ids}
    if not ids:
        return []
    now = timezone.now() if now is None else now  # defensive: a clock always exists
    cutoff = now - timedelta(minutes=crowd_weighting.DECAY_END_MIN)
    rows = (
        CrowdReport.objects.filter(stage_id__in=ids)
        .filter(
            Q(last_bumped_at__gte=cutoff)
            | Q(last_bumped_at__isnull=True, created_at__gte=cutoff)
        )
        .order_by("stage_id", "pk")
    )
    shaped = []
    for row in rows:
        item = {
            "pk": row.pk,
            "stage_id": row.stage_id,
            "type": row.kind,  # stored enum VALUE (hard rule)
            "count": int(row.merged_count),
            "created_at": row.created_at,
            "last_seen_at": row.last_bumped_at or row.created_at,
            "note": row.note,
        }
        age = crowd_weighting.age_min(item, now=now)
        if crowd_weighting.decay_factor(age) <= 0.0:
            continue  # expired — never dress an old condition as current
        shaped.append(
            {
                "stage_id": item["stage_id"],
                "type": item["type"],
                "count": item["count"],
                "age_min": round(age, 1),
                "first_seen_at": item["created_at"].isoformat(),
                "note": item["note"],
                "report_id": item["pk"],
            }
        )
    return shaped


# --------------------------------------------------------------- resolution
def resolve_stage_query(query: str):
    """(stage_row | None, suggestions) — icase/nickname via the routes
    search PUBLIC door (trigram over stage names + alias table). The
    exact case-insensitive name wins; otherwise the door's best trigram
    hit does. Nothing is guessed: no match → (None, door's hits)."""
    query = (query or "").strip()
    if not query:
        return None, []
    _routes, stages = routes_search.search_network(query)
    for row in stages:
        if str(row["name"]).strip().lower() == query.lower():
            return row, stages[:MAX_SUGGESTIONS]
    return (stages[0], stages[:MAX_SUGGESTIONS]) if stages else (None, [])


# ------------------------------------------------------------------ payload
def _disruption_lines(*sections) -> list[str]:
    """Translated, CITED lines for every community condition on a plan.

    Lines are collected across ALL plan sections (the current plan and
    its alternates) and deduped: the same flooded leg is cited once no
    matter which section's legs carried it. The rider-count sentence
    comes from crowd.display.rider_line (the one public rendering of
    community voice); this module only appends the age/first-seen
    citation and the exact delay the planner applied — all from values
    the crowd payload already carries (never invented).
    """
    lines: list[str] = []
    for section in sections:
        for leg in section["legs"]:
            stage_name = leg.get("to_stage_name") or leg.get("from_stage_name") or ""
            for d in leg.get("disruptions", []):
                core = rider_line(
                    d["kind"], d["count"],
                    stage_name=d.get("stage_name") or stage_name,
                )
                tail = _("%(core)s — since %(first_seen)s (%(age).0f min ago)") % {
                    "core": core,
                    "first_seen": d["first_seen_at"],
                    "age": d["age_min"],
                }
                # NOTE: blocking conditions (a closure) apply no delay —
                # the honest tail then carries no "+X min" clause at all
                # rather than a fake "+0 min".
                if d["delay_min"] > 0:
                    tail = _(
                        "%(tail)s; planned as +%(delay).0f min."
                    ) % {"tail": tail, "delay": d["delay_min"]}
                if tail not in lines:
                    lines.append(tail)
    return lines


def _via_route_names(legs: Iterable[dict]) -> list[str]:
    """Matatu route names a plan RIDES, in first-appearance order (plain
    data — name strings, no translation)."""
    seen: list[str] = []
    for leg in legs:
        name = leg.get("route_name")
        if name and name not in seen:
            seen.append(name)
    return seen


def _fare_display(lo: int, hi: int) -> str:
    """The single display form of a fare RANGES summary (numbers only in
    the graph; the currency sentence is translated here)."""
    if lo == hi:
        return _("KSh %(lo)s") % {"lo": lo}
    return _("KSh %(lo)s–%(hi)s") % {"lo": lo, "hi": hi}


def _label_disruptions(sections: Iterable[dict]) -> None:
    """Give each cited disruption its translated kind label IN PLACE —
    the pure planner passes kind VALUES through (hard rule: values are
    persisted, labels are display vocabulary); the label is resolved
    here at payload time through crowd's own ReportType."""
    for section in sections:
        for leg in section["legs"]:
            for d in leg["disruptions"]:
                d["type_label"] = str(ReportType(d["kind"]).label)


def journey_payload(from_stage: dict, to_stage: dict, *, now: datetime) -> dict:
    """One complete JSON-stable payload: plan + alternates + cited
    conditions + fare ranges. The page and its JSON mirror share THIS —
    one truth, no drift (the alerts pattern)."""
    stamp = timezone.now() if now is None else now  # views inject the clock
    base = {
        "from_stage": {"id": from_stage["id"], "name": from_stage["name"]},
        "to_stage": {"id": to_stage["id"], "name": to_stage["name"]},
        "generated_at": stamp.isoformat(),
    }
    if int(from_stage["id"]) == int(to_stage["id"]):
        return dict(
            base, status="TRIVIAL",
            message=MSG_ALREADY_THERE % {"stage": from_stage["name"]},
        )

    graph_rows = network_rows()
    stage_ids = {row["stage_id"] for row in graph_rows}
    crowd_rows = crowd_rows_for(stage_ids, now=stamp)
    if crowd_rows:
        stage_names = {row["stage_id"]: row["stage_name"] for row in graph_rows}
        for row in crowd_rows:  # the citation must name the stage it reports
            row["stage_name"] = stage_names.get(row["stage_id"], "")
    network = graph.build_graph(graph_rows, fare_rows(), crowd_rows)
    result = graph.plan(network, int(from_stage["id"]), int(to_stage["id"]))
    if result is None:  # disconnected — honest empty, never a fake plan
        return dict(base, status="NO_ROUTE", message=MSG_NO_ROUTE)

    sections = [result]
    for key in ("usual", "avoiding"):
        section = result.get(key)
        if section:
            sections.append(section)
    _label_disruptions(sections)
    for key in ("usual", "avoiding"):
        section = result.get(key)
        if section:  # each plan section carries its own "riden route
            # names" for the "alternative via X" line — plain data;
            # translated wording stays in the template.
            section["via_route_names"] = _via_route_names(section["legs"])
    result["via_route_names"] = _via_route_names(result["legs"])
    legs = result["legs"]
    usual = result.get("usual")
    avoiding = result.get("avoiding")
    lo, hi, fare_key = graph.aggregate_fares(legs)
    return dict(
        base,
        status="OK",
        plan=result,
        fares={"min_total": lo, "max_total": hi, "confidence": fare_key},
        fare_display=_fare_display(lo, hi),
        total_minutes=result["total_minutes"],
        disruption_lines=_disruption_lines(
            result, usual or {"legs": []}, avoiding or {"legs": []}
        ),
        messages=[MSG_HOW_TIMES, MSG_HOW_FARES],
    )
