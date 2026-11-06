"""serialization.py — the map-overlay payload seam (Prompt 30 → 28).

THE 28 SEAM: Prompt 28's live map owns stage geometry and draws overlays;
when it lands it should call crowd_overlay_for_stage() per stage (or per
route) and render the returned dicts AS-IS. This module is therefore
shape-stable, tested against a mocked consumer, and NEVER changes to suit
a page template — views use it too by passing their own scorer, so the
map and the HTML feed show the same weighted truth.

HONESTY CONTRACT baked into the shape (asserted by tests):
- NO coordinates, NO positions: keys are type/count/age/weight — the
  overlay is pinned to a stage the CALLER already has (stage_id);
- NO rider identities: "reporter"/"username" keys do not exist — only
  count ("N riders report…");
- expired reports (weight 0.0 at the injected now) are DROPPED — old
  conditions never dress up as current;
- sorting is weight DESC, then freshest evidence; every entry carries
  confidence = CONFIDENCE_COMMUNITY (Confidence.COMMUNITY_REPORT).
"""
from __future__ import annotations

from datetime import datetime

from .display import rider_line
from .models import Confidence, CrowdReport, ReportType
from .weighting import weight_rows


def crowd_overlay_for_stage(stage, *, now: datetime, scorer) -> list[dict]:
    """Current, weighted crowd overlay for ONE stage.

    Args:
      stage: routes.Stage — the anchor; its pk goes into the payload and
             NOT a position (28 owns the geometry/markers).
      now:   INJECTED clock (pure read path; the view injects timezone.now()).
      scorer: trust_scorer(reporter) -> 0..1 — services.make_report_scorer()
             in production, a mock in 28's/this app's tests.

    Returns a list of dicts sorted by weight DESC (see module docstring
    for the exact key set and its invariants).
    """
    from .weighting import DECAY_END_MIN

    rows = [
        {
            "pk": r.pk,
            "reporter": r.reporter,  # passed to the scorer only — never serialized
            "kind": r.kind,
            "note": r.note,
            "destination_hint": r.destination_hint,
            "merged_count": r.merged_count,
            "created_at": r.created_at,
            "last_seen_at": r.last_bumped_at or r.created_at,
            "route": r.route,
            "stage": stage,
        }
        for r in CrowdReport.objects.filter(stage=stage).select_related(
            "reporter", "route"
        )
    ]
    scored = weight_rows(rows, trust_scorer=scorer, now=now)

    payload = []
    for r in scored:
        if r["weight"] <= 0:
            continue  # expired at `now` — honest silence, never a stale pin
        route = r["route"]
        payload.append(
            {
                "stage_id": stage.pk,
                "stage_name": stage.name,
                "type": r["kind"],
                "type_label": str(ReportType(r["kind"]).label),
                "count": r["merged_count"],
                "age_min": round(r["age_min"], 2),
                "weight": r["weight"],
                "confidence": Confidence.COMMUNITY_REPORT.value,
                "first_seen_at": r["created_at"].isoformat(),
                "last_seen_at": r["last_seen_at"].isoformat(),
                "note": r["note"],
                "destination_hint": r["destination_hint"],
                "route_id": route.pk if route is not None else None,
                "route_name": route.name_en if route is not None else "",
                "display": rider_line(
                    r["kind"],
                    r["merged_count"],
                    stage_name=stage.name,
                    route_name=route.name_en if route is not None else None,
                ),
            }
        )
    return payload
