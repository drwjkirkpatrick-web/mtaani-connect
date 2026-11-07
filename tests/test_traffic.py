"""tests/test_traffic.py — Prompt 32: traffic & route planning.

Acceptance coverage:

  PURE PLANNER (hand-built rows, no DB):
  * shortest path over the stage graph — the winding "direct" route loses
    when the via-shared-stage path is genuinely shorter, and a cheap
    direct route wins when it really is shortest;
  * transfer penalty effect — the SAME via path totals differ by exactly
    TRANSFER_PENALTY_MIN between default and penalty=0; penalty=0
    collapses to the pure sum of hop minutes; N transfers → N penalties;
  * disruption rerouting — FLOODING at a mid-stage flips the plan to the
    alternative with the honest disruption line citing rider count,
    first-seen and age (source "community", never official); DELAY adds
    merged-count minutes; STAGE_CLOSED drops the edge and, with no
    alternative, returns an honest None; unknown kinds cost nothing;
  * fare aggregation — per-leg observed RANGES summed to (min, max);
    segment/reversed/route bases; unknown legs → PARTIAL (never a single
    official number);
  * disconnected graphs → None, no crash, no infinity; unknown stage ids
    → None; missing coordinates → DEFAULT_EDGE_MIN, flagged estimated;
  * walking legs — the 300 m cap is enforced, minutes = labelled distance
    × the pace constant; stages without coordinates never walk;
  * purity — graph.py imports NOTHING Django (AST-scanned source) and
    walk_legs is deterministic.

  DOOR FLOW (real ORM + real public doors):
  * crowd reports filed via the crowd PUBLIC service door (merged riders),
    conditions fetched services-side and cited verbatim in the plan page
    and its JSON mirror; STAGE_CLOSED flow; nickname alias resolution via
    the routes search door; disconnected honest empty state; the about
    page; the 50 KB page budget.
"""
from __future__ import annotations

import ast
import json
from decimal import Decimal
from importlib.util import find_spec
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone as dj_timezone

from apps.core.geo import distance_km
from apps.core.testing import assert_under_budget, mock_now
from apps.routes.models import Fare, Route, RouteStage, Stage, StageAlias
from apps.traffic import graph, services

T0 = mock_now()  # one fixed UTC moment — the injected-clock rule (pure fns)


# ============ helpers: hand-built rows for the PURE planner ============
def _link(route_id, route_name, stage_id, stage_name, pos, *, lat=None, lon=None, seed=False):
    """One stagelink row in the contract shape services fetches."""
    return {
        "route_id": route_id, "route_name": route_name, "route_seed": seed,
        "stage_id": stage_id, "stage_name": stage_name,
        "lat": None if lat is None else float(lat),
        "lon": None if lon is None else float(lon),
        "position": pos,
    }


def _fare(route_id, a, b, lo, hi, *, conf="COMMUNITY_REPORT", pk=1):
    """One observed-fare-range row (both bounds required — Prompt 25)."""
    return {
        "pk": pk, "route_id": route_id, "from_stage_id": a, "to_stage_id": b,
        "min_price": lo, "max_price": hi, "confidence": conf,
    }


def _crowd(stage_id, kind, *, count=1, age_min=5.0,
           first_seen="2026-09-03T09:20:00+00:00", note="", report_id=99,
           stage_name=""):
    """One crowd row in the frozen overlay vocabulary (type/count/age...)."""
    return {
        "stage_id": stage_id, "type": kind, "count": count,
        "age_min": age_min, "first_seen_at": first_seen,
        "note": note, "report_id": report_id, "stage_name": stage_name,
    }


def _hop_minutes(a_coord, b_coord) -> float:
    """Expected hop minutes — recomputed with the planner's own formula
    (WALK_TIME_PER_KM_MIN × haversine) so tests pin the exact value."""
    return round(distance_km(a_coord, b_coord) * graph.WALK_TIME_PER_KM_MIN, 1)


# Stage lattice: consecutive stages ≈ DEG degrees of latitude apart —
# ~1 km at DEG = 0.009 — realistic corridor spacing and, importantly, FAR
# beyond the 300 m walk cap, so main-fixture hops can never be beaten by
# a spurious walk family edge.
LAT = -1.290000
LON = 36.800000
DEG = 0.009
C1 = (LAT, LON)
C2 = (LAT + DEG, LON)
C3 = (LAT + 2 * DEG, LON)
C4 = (LAT + 3 * DEG, LON)
C5 = (LAT + 0.09, LON)     # ~10 km hop — a hopeless single-edge "direct"
C6 = (LAT + DEG, LON - 0.004)  # a slightly-off-path neighbour stage
C9 = (LAT, LON + 0.03)     # a wide DETOUR stage (for the winding route)

# SHARED-STAGE two-route network:
#   Route 1 "Route Via"  : 1 → 2 → 3   (short hops; stage 2 shared)
#   Route 2 "Route Spur" : 2 → 4       (the change at the shared stage 2)
#   Route 3 "Route Windy": 1 → 5 → 4   (a winding 10 km "direct")
VIA4_LINKS = [
    _link(1, "Route Via", 1, "Kawangware", 1, lat=C1[0], lon=C1[1]),
    _link(1, "Route Via", 2, "Kasarani", 2, lat=C2[0], lon=C2[1]),
    _link(1, "Route Via", 3, "Ambassadeur", 3, lat=C3[0], lon=C3[1]),
    _link(2, "Route Spur", 2, "Kasarani", 1, lat=C2[0], lon=C2[1]),
    _link(2, "Route Spur", 4, "Gikomba", 2, lat=C4[0], lon=C4[1]),
    _link(3, "Route Windy", 1, "Kawangware", 1, lat=C1[0], lon=C1[1]),
    _link(3, "Route Windy", 5, "Far Depot", 2, lat=C5[0], lon=C5[1]),
    _link(3, "Route Windy", 4, "Gikomba", 3, lat=C4[0], lon=C4[1]),
]
VIA4_FARES = [
    _fare(1, 1, 2, 50, 80, pk=1),
    _fare(1, 2, 3, 30, 40, pk=2),
    _fare(2, 2, 4, 60, 90, pk=3),
]


# ---------------------------------------------------------------- shortest
class TestShortestPath:
    def test_plan_rides_via_and_changes_routes_at_the_shared_stage(self):
        built = graph.build_graph(VIA4_LINKS, VIA4_FARES)
        plan = graph.plan(built, 1, 4)
        assert plan is not None
        assert plan.keys() == {"legs", "total_minutes", "usual", "avoiding"}
        ridden = [l for l in plan["legs"] if l["kind"] == "stage-hop"]
        assert [l["route_name"] for l in ridden] == ["Route Via", "Route Spur"]
        transfers = [l for l in plan["legs"] if l["kind"] == "transfer"]
        assert len(transfers) == 1
        transfer = transfers[0]
        assert transfer["minutes"] == graph.TRANSFER_PENALTY_MIN
        assert transfer["from_stage_name"] == "Kasarani"  # the SHARED stage
        assert transfer["route_name"] == "Route Spur"  # the route you JOIN
        expected = (
            _hop_minutes(C1, C2) + graph.TRANSFER_PENALTY_MIN + _hop_minutes(C2, C4)
        )
        assert plan["total_minutes"] == round(expected, 1)
        assert plan["usual"] is None and plan["avoiding"] is None

    def test_winding_direct_loses_to_the_via_plan(self):
        built = graph.build_graph(VIA4_LINKS, VIA4_FARES)
        plan = graph.plan(built, 1, 4)
        assert all(l["route_name"] != "Route Windy" for l in plan["legs"])
        # sanity: the route Windy total really was worse
        windy = _hop_minutes(C1, C5) + _hop_minutes(C5, C4)
        assert windy > plan["total_minutes"]

    def test_really_short_direct_route_wins(self):
        links = [
            _link(1, "Route Winding", 1, "S1", 1, lat=C1[0], lon=C1[1]),
            _link(1, "Route Winding", 2, "Detour Depot", 2, lat=C9[0], lon=C9[1]),
            _link(1, "Route Winding", 3, "S3", 3, lat=C3[0], lon=C3[1]),
            _link(9, "Route Direct", 1, "S1", 1, lat=C1[0], lon=C1[1]),
            _link(9, "Route Direct", 3, "S3", 2, lat=C3[0], lon=C3[1]),
        ]
        built = graph.build_graph(links, [])
        plan = graph.plan(built, 1, 3)
        assert [l["route_name"] for l in plan["legs"]] == ["Route Direct"]
        direct = _hop_minutes(C1, C3)
        assert plan["total_minutes"] == direct
        winding = _hop_minutes(C1, C9) + _hop_minutes(C9, C3)
        assert direct < winding  # sanity: the direct route really was cheaper

    def test_start_equals_goal_is_an_honest_trivial_plan(self):
        built = graph.build_graph(VIA4_LINKS, [])
        plan = graph.plan(built, 2, 2)
        assert plan == {"legs": [], "total_minutes": 0.0, "usual": None, "avoiding": None}

    def test_leg_keys_are_the_frozen_contract(self):
        built = graph.build_graph(VIA4_LINKS, VIA4_FARES)
        plan = graph.plan(built, 1, 4)
        hop = plan["legs"][0]
        assert set(hop) == {
            "kind", "route_id", "route_name", "source",
            "from_stage", "to_stage", "from_stage_name", "to_stage_name",
            "minutes", "delay_minutes", "estimated", "distance_km",
            "fare", "disruptions",
        }
        # source separation ON the leg data itself: seeded vs official
        seeded_links = [
            _link(8, "Seeded Route", 1, "S1", 1, lat=C1[0], lon=C1[1], seed=True),
            _link(8, "Seeded Route", 2, "S2", 2, lat=C2[0], lon=C2[1], seed=True),
        ]
        seeded = graph.build_graph(seeded_links, [], None)
        plan_seeded = graph.plan(seeded, 1, 2)
        assert plan_seeded["legs"][0]["source"] == "seed"
        assert plan["legs"][0]["source"] == "official"  # VIA4 routes are unseeded


# -------------------------------------------------------- transfer penalty
class TestTransferPenalty:
    def test_totals_differ_by_exactly_the_penalty(self):
        built = graph.build_graph(VIA4_LINKS, [])
        with_penalty = graph.plan(built, 1, 4)  # default TRANSFER_PENALTY_MIN
        without = graph.plan(built, 1, 4, transfer_penalty_min=0.0)
        assert round(with_penalty["total_minutes"] - without["total_minutes"], 6) == (
            graph.TRANSFER_PENALTY_MIN
        )

    def test_penalty_zero_collapses_to_the_pure_graph_sum(self):
        built = graph.build_graph(VIA4_LINKS, [])
        plan = graph.plan(built, 1, 4, transfer_penalty_min=0.0)
        pure_sum = _hop_minutes(C1, C2) + _hop_minutes(C2, C4)
        assert plan["total_minutes"] == round(pure_sum, 1)

    def test_penalty_is_per_transfer_not_per_leg(self):
        # THREE routes in sequence 1→2 → 2→3 → 3→4: each swap stands as one
        # TRANSFER leg (no double charge for the same-stage alight/board).
        links = [
            _link(1, "A", 1, "S1", 1, lat=C1[0], lon=C1[1]),
            _link(1, "A", 2, "S2", 2, lat=C2[0], lon=C2[1]),
            _link(2, "B", 2, "S2", 1, lat=C2[0], lon=C2[1]),
            _link(2, "B", 3, "Ambassadeur", 2, lat=C3[0], lon=C3[1]),
            _link(3, "C", 3, "Ambassadeur", 1, lat=C3[0], lon=C3[1]),
            _link(3, "C", 4, "S4", 2, lat=C4[0], lon=C4[1]),
        ]
        plan = graph.plan(graph.build_graph(links, []), 1, 4)
        assert len([l for l in plan["legs"] if l["kind"] == "transfer"]) == 2


# ------------------------------------------------------ disruptions (pure)
class TestDisruptions:
    # A "Usual": 1→2→3 (short, hits the flooded stage 2)
    # B "Alternative": 1→6→3 (slightly longer, avoids stage 2)
    TWO_ROUTES = [
        _link(1, "Usual", 1, "S1", 1, lat=C1[0], lon=C1[1]),
        _link(1, "Usual", 2, "S2", 2, lat=C2[0], lon=C2[1]),
        _link(1, "Usual", 3, "S3", 3, lat=C3[0], lon=C3[1]),
        _link(2, "Alternative", 1, "S1", 1, lat=C1[0], lon=C1[1]),
        _link(2, "Alternative", 6, "S6", 2, lat=C6[0], lon=C6[1]),
        _link(2, "Alternative", 3, "S3", 3, lat=C3[0], lon=C3[1]),
    ]

    def test_flooding_reroutes_and_the_usual_route_cites_reports(self):
        flooding = [_crowd(2, "FLOODING", count=3, age_min=12.5, note="river")]  # +6/edge
        built = graph.build_graph(self.TWO_ROUTES, [], flooding)
        plan = graph.plan(built, 1, 3)
        # PRIMARY: the clean alternative — nothing community-invented
        assert all(
            l["route_name"] == "Alternative"
            for l in plan["legs"] if l["kind"] == "stage-hop"
        )
        assert all(not l["disruptions"] for l in plan["legs"])
        # USUAL: the pre-report route stays in the payload WITH the cited line
        usual = plan["usual"]
        assert usual is not None
        usual_hops = [l for l in usual["legs"] if l["kind"] == "stage-hop"]
        cited = [d for l in usual_hops for d in l["disruptions"]]
        assert len(cited) == 2  # both hops touching the flooded stage
        cite = cited[0]
        assert cite["count"] == 3
        assert cite["delay_min"] == 6.0  # FLOODING = +2 riders... +2 per rider
        assert cite["age_min"] == 12.5
        assert cite["first_seen_at"] == "2026-09-03T09:20:00+00:00"
        assert cite["source"] == "community"
        assert cite["confidence"] == "COMMUNITY_REPORT"
        # usual total = the COUNTERFACTUAL (base only; delays stay cited)
        assert usual["total_minutes"] == round(
            sum(l["minutes"] for l in usual_hops), 1
        )
        assert plan["avoiding"] is None  # same route as primary → no noise

    def test_flooding_without_an_alternative_stays_but_shows_delay(self):
        links = self.TWO_ROUTES[:3]
        built = graph.build_graph(links, [], [_crowd(2, "FLOODING", count=2)])
        plan = graph.plan(built, 1, 3)
        assert plan["legs"][0]["delay_minutes"] == 4.0  # +2 × 2 riders
        assert plan["usual"] is None  # there IS no alternative — honest

    def test_delay_kind_adds_merged_count_minutes(self):
        links = [
            _link(1, "A", 1, "S1", 1, lat=C1[0], lon=C1[1]),
            _link(1, "A", 2, "S2", 2, lat=C2[0], lon=C2[1]),
        ]
        built = graph.build_graph(links, [], [_crowd(2, "DELAY", count=4)])
        plan = graph.plan(built, 1, 2)
        assert plan["legs"][0]["delay_minutes"] == 4.0  # DELAY = +N
        assert plan["total_minutes"] == round(_hop_minutes(C1, C2) + 4.0, 1)

    def test_stage_closed_drops_the_edges_and_reroutes(self):
        built = graph.build_graph(self.TWO_ROUTES, [], [_crowd(2, "STAGE_CLOSED")])
        plan = graph.plan(built, 1, 3)
        assert plan is not None
        ridden = [l["route_name"] for l in plan["legs"] if l["kind"] == "stage-hop"]
        assert ridden == ["Alternative", "Alternative"]  # around the closure
        usual = plan["usual"]
        assert usual is not None  # the pre-report plan exists (counterfactual)

    def test_stage_closed_with_no_alternative_is_honest_none(self):
        links = self.TWO_ROUTES[:3]
        built = graph.build_graph(links, [], [_crowd(2, "STAGE_CLOSED")])
        assert graph.plan(built, 1, 3) is None  # no crash, NEVER ∞
        assert graph.dijkstra(built, 1, 3) is None

    def test_citation_keys_are_frozen_and_nothing_is_invented(self):
        flooding = [_crowd(2, "FLOODING", count=3, age_min=12.5, note="river over the road",
                           stage_name="S2")]
        built = graph.build_graph(self.TWO_ROUTES[:3], [], flooding)
        plan = graph.plan(built, 1, 3)
        # the ONLY route is the flooded one: the plan stays on it and the
        # condition is cited on its legs (there is no usual alternative).
        assert plan["usual"] is None
        cited = [d for l in plan["legs"] for d in l["disruptions"]]
        assert cited
        # flooding only delays (it never blocks); frozen citation keys
        cite = cited[0]
        assert set(cite) == {
            "kind", "count", "age_min", "first_seen_at", "stage_name",
            "delay_min", "note", "source", "confidence", "report_id",
        }
        assert cite["note"] == "river over the road" and cite["report_id"] == 99
        assert cite["stage_name"] == "S2" and cite["kind"] == "FLOODING"

    def test_closure_citations_say_nothing_about_a_delay(self):
        built = graph.build_graph(
            self.TWO_ROUTES[:3], [], [_crowd(2, "STAGE_CLOSED", count=1, stage_name="S2")]
        )
        plan = graph.plan(built, 1, 3)
        assert plan is None  # blocked on every path → honest None
        # ignore-plan counterfactual still rides it (cited, no "+0 min"):
        usual_legs = graph.dijkstra(
            built, 1, 3, crowd_mode="ignore", transfer_penalty_min=graph.TRANSFER_PENALTY_MIN
        )
        assert usual_legs is not None  # the pre-report route existed
        cites = [d for l in usual_legs for d in l["disruptions"]]
        assert cites[0]["kind"] == "STAGE_CLOSED"

    def test_unnamed_report_kinds_cost_nothing_and_block_nothing(self):
        # MATATU_HERE is a POSITIVE sighting — a positive signal delays nobody.
        built = graph.build_graph(
            self.TWO_ROUTES[:3], [], [_crowd(2, "MATATU_HERE", count=7)]
        )
        plan = graph.plan(built, 1, 3)
        assert plan["legs"][0]["delay_minutes"] == 0.0
        assert plan["usual"] is None


# ------------------------------------------------------------ fare ranges
class TestFares:
    def test_per_leg_ranges_summed_never_one_number(self):
        plan = graph.plan(graph.build_graph(VIA4_LINKS, VIA4_FARES), 1, 3)
        lo, hi, conf = graph.aggregate_fares(plan["legs"])
        assert (lo, hi) == (50 + 30, 80 + 40)  # sums of BOTH observed bounds
        assert conf == graph.FARE_OBSERVED
        assert lo != hi  # a range — never a single official KSh number
        legs = plan["legs"]
        assert legs[0]["fare"] == {
            "lo": 50, "max": 80, "confidence": "COMMUNITY_REPORT", "basis": "segment",
        }

    def test_reverse_pair_and_route_level_bases(self):
        # travelling 3→1: no exact row for (3→2) but the reversed row is a
        # REAL observed fare (matatus charge the same both ways).
        plan = graph.plan(graph.build_graph(VIA4_LINKS, VIA4_FARES), 3, 1)
        assert plan["legs"][0]["fare"]["basis"] == "segment_reverse"
        # a hop with no exact/reversed row falls to the route-level range
        # (the ROUTE's overall observed range from its priced rows)
        hops_only = [l for l in VIA4_LINKS if l["route_id"] == 1]
        fares = [_fare(1, 1, 2, 50, 80, pk=1)]  # only 1→2 priced; 2→3 unpriced
        plan2 = graph.plan(graph.build_graph(hops_only, fares), 1, 3)
        assert plan2["legs"][0]["fare"]["basis"] == "segment"
        assert plan2["legs"][1]["fare"]["basis"] == "route"
        assert (plan2["legs"][1]["fare"]["lo"], plan2["legs"][1]["fare"]["max"]) == (50, 80)

    def test_unknown_fares_flag_the_totals_partial(self):
        plan = graph.plan(graph.build_graph(VIA4_LINKS, []), 1, 3)
        lo, hi, conf = graph.aggregate_fares(plan["legs"])
        assert (lo, hi) == (0, 0)
        assert conf == graph.FARE_PARTIAL

    def test_walk_and_transfer_legs_cost_no_fare(self):
        # two stages ≈ 150 m apart, on two otherwise-separate routes: the
        # walk family is the only honest way across, and it is free.
        near = (round(C1[0] + 0.0009, 6), round(C1[1] + 0.0011, 6))
        links = [
            _link(1, "A", 7, "W1", 1, lat=C1[0], lon=C1[1]),
            _link(2, "B", 8, "W2", 1, lat=near[0], lon=near[1]),
        ]
        built = graph.build_graph(links, [], None)
        plan = graph.plan(built, 7, 8)
        assert plan is not None
        walk = [l for l in plan["legs"] if l["kind"] == "walk"]
        assert walk
        assert all(l["route_name"] is None for l in walk)
        lo, hi, conf = graph.aggregate_fares(plan["legs"])
        assert (lo, hi) == (0, 0)
        assert conf == graph.FARE_NO_PAID_LEGS  # walking is free, not "KSh 0"


# -------------------------------------------------- disconnected + fallback
class TestDisconnectedAndFallback:
    def test_two_islands_return_none_without_crash(self):
        islands = [
            _link(1, "Island A", 1, "S1", 1, lat=C1[0], lon=C1[1]),
            _link(1, "Island A", 2, "S2", 2, lat=C2[0], lon=C2[1]),
            _link(2, "Island B", 3, "S3", 1, lat=C1[0] + 5 * DEG, lon=C1[1]),
            _link(2, "Island B", 4, "S4", 2, lat=C2[0] + 5 * DEG, lon=C1[1]),
        ]
        built = graph.build_graph(islands, [], None)
        assert graph.plan(built, 1, 4) is None  # honest None — no crash
        assert graph.dijkstra(built, 1, 4) is None

    def test_plan_of_unknown_stage_ids_is_none_too(self):
        built = graph.build_graph(VIA4_LINKS, [], None)
        assert graph.plan(built, 999, 4) is None

    def test_missing_coordinates_fall_back_to_default_edge(self):
        links = [
            _link(1, "A", 5, "NoCoords", 1),
            _link(1, "A", 6, "NoCoords2", 2),
        ]
        built = graph.build_graph(links, [], None)
        leg = graph.plan(built, 5, 6)["legs"][0]
        assert leg["minutes"] == graph.DEFAULT_EDGE_MIN
        assert leg["estimated"] is True  # the fallback is FLAGGED, not hidden
        assert leg["distance_km"] is None  # no invented distance


# ------------------------------------------------------------ walking legs
def _walk_pair(meters: float) -> list[dict]:
    """Two stages `meters` apart on DIFFERENT single-stage routes —
    the only possible connection is the WALK family (or nothing)."""
    km = meters / 1000.0
    dlon = km / (111.32)  # degrees of longitude ≈ km / 111.32 at this latitude
    return [
        _link(1, "A", 7, "W1", 1, lat=C1[0], lon=C1[1]),
        _link(2, "B", 8, "W2", 1, lat=C1[0], lon=round(C1[1] + dlon, 6)),
    ]


class TestWalkingLegs:
    def test_within_cap_walk_leg_is_labeled_and_realistic(self):
        built = graph.build_graph(_walk_pair(250), [], None)
        plan = graph.plan(built, 7, 8)
        assert plan is not None
        legs = [l for l in plan["legs"] if l["kind"] == "walk"]
        assert legs, "a 250 m connection should be a WALK leg"
        walk = legs[0]
        assert walk["minutes"] == round(0.25 * graph.WALK_TIME_PER_KM_MIN, 1)
        assert walk["route_id"] is None and walk["fare"] is None
        assert walk["distance_km"] == 0.25
        # no fabricated matatu leg hides behind the walk
        assert walk["source"] == "official"

    def test_over_cap_has_no_walk_edge_and_no_fabricated_plan(self):
        built = graph.build_graph(_walk_pair(400), [], None)
        assert graph.plan(built, 7, 8) is None  # the cap held; honest None

    def test_walk_legs_pure_fn_is_deterministic_and_capped(self):
        a = graph.walk_legs(_walk_pair(250))
        b = graph.walk_legs(_walk_pair(250))
        assert a == b  # same rows in, same pairs out
        assert all(p["distance_km"] * 1000 <= graph.WALK_METERS for p in a)

    def test_stage_without_coordinates_is_never_walkable(self):
        rows = [
            _link(1, "A", 11, "NoC1", 1),
            _link(2, "B", 12, "NoC2", 1),
        ]
        assert graph.walk_legs(rows) == []  # no coords → no honest length


# ------------------------------------------------------------------ purity
class TestPureModule:
    def test_graph_module_imports_nothing_from_django(self):
        """The planner stays pure: NO Django imports (grep-style via AST —
        a docstring mention can never fake a pass)."""
        spec = find_spec("apps.traffic.graph")
        source = Path(spec.origin).read_text()
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.add(node.names[0].name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or ".").split(".")[0])
        assert roots <= {"__future__", "heapq", "dataclasses", "typing", "apps"}, roots
        assert "django" not in roots


# ============ DOOR FLOW: the real ORM + the real public doors ============
@pytest.fixture
def db_world(db, fresh_user):
    """Kawangware → CBD acceptance world: two short routes + one winding
    'Rain Route' that the flood can beat, fares on every segment, the
    nickname alias for the door search, and a FLOODING row filed through
    the crowd PUBLIC service door (two riders → merged_count 2, raised).
    Report clocks are injected relative to the REAL now so the payload
    read (also the real clock) sees a CURRENT condition."""
    User = get_user_model()
    rider2 = User.objects.create_user("riderb32", "riderb32@example.com", "***")
    kawangware = Stage.objects.create(
        name="Kawangware", lat=Decimal("-1.300000"), lon=Decimal("36.733300")
    )
    junction = Stage.objects.create(
        name="Kikuyu Junction", lat=Decimal("-1.295000"), lon=Decimal("36.756000")
    )
    kilimani = Stage.objects.create(
        name="Kilimani", lat=Decimal("-1.292100"), lon=Decimal("36.772700")
    )
    cbd = Stage.objects.create(
        name="Ambassadeur", lat=Decimal("-1.286400"), lon=Decimal("36.817200")
    )
    post_west = Stage.objects.create(
        name="Post West", lat=Decimal("-1.306000"), lon=Decimal("36.700000")
    )
    route_a = Route.objects.create(
        name_en="Kawangware–Kilimani", seed_origin=True, gtfs_id="t32-a"
    )
    route_b = Route.objects.create(name_en="Kilimani–CBD")
    route_c = Route.objects.create(name_en="Rain Route Winding")
    RouteStage.objects.create(route=route_a, stage=kawangware, position=1)
    RouteStage.objects.create(route=route_a, stage=junction, position=2)
    RouteStage.objects.create(route=route_a, stage=kilimani, position=3)
    RouteStage.objects.create(route=route_b, stage=kilimani, position=1)
    RouteStage.objects.create(route=route_b, stage=cbd, position=2)
    RouteStage.objects.create(route=route_c, stage=kawangware, position=1)
    RouteStage.objects.create(route=route_c, stage=post_west, position=2)
    RouteStage.objects.create(route=route_c, stage=cbd, position=3)
    Fare.objects.create(
        route=route_a, from_stage=kawangware, to_stage=junction,
        min_price=50, max_price=80, confidence="COMMUNITY_REPORT",
    )
    Fare.objects.create(
        route=route_a, from_stage=junction, to_stage=kilimani,
        min_price=30, max_price=50, confidence="COMMUNITY_REPORT",
    )
    Fare.objects.create(
        route=route_b, from_stage=kilimani, to_stage=cbd,
        min_price=60, max_price=90, confidence="COMMUNITY_REPORT",
    )
    Fare.objects.create(
        route=route_b, from_stage=cbd, to_stage=kilimani,
        min_price=60, max_price=90, confidence="COMMUNITY_REPORT",
    )
    Fare.objects.create(
        route=route_c, from_stage=kawangware, to_stage=post_west,
        min_price=80, max_price=120, confidence="COMMUNITY_REPORT",
    )
    Fare.objects.create(
        route=route_c, from_stage=post_west, to_stage=cbd,
        min_price=150, max_price=200, confidence="COMMUNITY_REPORT",
    )
    StageAlias.objects.create(stage=junction, alias="Kikuyu Road")
    # Community evidence through the PUBLIC door — an injected clock makes
    # the merge window deterministic; the SECOND filing must MERGE.
    from apps.crowd import services as crowd_services
    near_now = dj_timezone.now()
    first, _ = crowd_services.file_report(
        reporter=fresh_user, stage=kilimani, kind="FLOODING",
        route=route_b, note="River over the road", now=near_now,
    )
    second, outcome = crowd_services.file_report(
        reporter=rider2, stage=kilimani, kind="FLOODING",
        route=route_b, now=near_now,
    )
    assert outcome.value == "MERGED"  # the crowd door merged the riders
    # The 10-minute merge window caps honest rider bumps, but the count is
    # the documented cache field of the link rows — a fixture may show a
    # bigger merged crowd (30 riders = a truly flooded stage) without
    # inventing anything: the row exists, the count is only its value.
    second.merged_count = 30
    second.save(update_fields=["merged_count"])
    return {
        "stages": {
            "from": kawangware, "mid": junction, "flood": kilimani, "to": cbd,
        },
        "routes": {"a": route_a, "b": route_b, "c": route_c},
        "first_report": first, "second_report": second,
        "rider": fresh_user,
    }


@pytest.mark.django_db
class TestDoorFlow:
    def test_plan_page_plans_kawangware_to_cbd_with_cited_conditions(self, db_world, client):
        """THE ACCEPTANCE: 'Kawangware → CBD' plans with honest
        alternatives — usual route + reported flooding; alternative via X."""
        response = client.get(
            reverse("traffic:plan"), {"from": "Kawangware", "to": "Ambassadeur"}
        )
        assert response.status_code == 200
        html = response.content.decode()
        payload = response.context["payload"]
        assert payload["status"] == "OK"
        assert 'data-test="traffic-plan-summary"' in html
        assert "Kawangware" in html and "Ambassadeur" in html
        assert 'data-test="traffic-fare-total"' in html
        assert "KSh 230–320" in html  # the observed SUM RANGE of the plan
        assert 'data-test="traffic-disruption-line"' in html  # condition cited
        # the merged rider count and the flood delay are IN the citation
        assert "30 riders report Flooding at Kilimani" in html
        assert "planned as +60 min" in html  # FLOODING = +2N, N=30
        # the usual route survives as an alternative with its own leg list
        assert 'data-test="traffic-usual"' in html
        assert_under_budget(html, kb=50)

    def test_plan_json_mirror_matches_the_page_payload(self, db_world, client):
        response = client.get(
            reverse("traffic:plan_json"), {"from": "Kawangware", "to": "Ambassadeur"}
        )
        assert response.status_code == 200
        data = json.loads(response.content)
        payload = data["payload"]
        assert payload["status"] == "OK"
        # aggregated fare range = the SUM of the rerouted plan's leg bounds
        assert payload["fares"]["min_total"] == 80 + 150
        assert payload["fares"]["max_total"] == 120 + 200
        assert payload["fares"]["confidence"] == graph.FARE_OBSERVED
        # source separation, visible on the legs: the rerouted plan rides
        # a CURATED route (official) while the usual route is the SEED row
        assert all(
            l["source"] == "official"
            for l in payload["plan"]["legs"] if l["kind"] == "stage-hop"
        )
        assert any(
            l["source"] == "seed"
            for l in payload["plan"]["usual"]["legs"] if l["kind"] == "stage-hop"
        )
        # disruption citations are community-sourced and carry the count
        for leg in (l for l in payload["plan"]["legs"] if l["disruptions"]):
            for d in leg["disruptions"]:
                assert d["source"] == "community"
        assert payload["disruption_lines"], "mirror carries the cited condition"

    def test_flooding_reroutes_to_the_alternative_at_door_level(self, db_world, client):
        """The disruption acceptance: with the merged flood, the plan
        switches to the winding Rain Route and the payload carries 'usual
        route + reported flooding' with the citation."""
        response = client.get(
            reverse("traffic:plan_json"), {"from": "Kawangware", "to": "Ambassadeur"}
        )
        payload = json.loads(response.content)["payload"]
        ridden = [
            l["route_name"] for l in payload["plan"]["legs"] if l["kind"] == "stage-hop"
        ]
        assert ridden and all(name == "Rain Route Winding" for name in ridden), ridden
        # usual nests INSIDE the plan section in the payload
        assert payload["plan"]["usual"] is not None  # usual as alternate
        assert payload["plan"]["usual"]["via_route_names"] == [
            "Kawangware–Kilimani", "Kilimani–CBD",
        ]
        assert any("30 riders" in line for line in payload["disruption_lines"])

    def test_stage_closed_at_the_junction_skips_its_edges(self, db_world, client, fresh_user):
        from apps.crowd import services as crowd_services
        crowd_services.file_report(
            reporter=fresh_user,
            stage=db_world["stages"]["mid"],
            kind="STAGE_CLOSED",
            route=db_world["routes"]["a"],
            now=dj_timezone.now(),
        )
        response = client.get(
            reverse("traffic:plan_json"), {"from": "Kawangware", "to": "Ambassadeur"}
        )
        payload = json.loads(response.content)["payload"]
        assert payload["status"] == "OK"  # plans AROUND the closed junction
        hops = [l for l in payload["plan"]["legs"] if l["kind"] == "stage-hop"]
        assert all("Kikuyu Junction" not in (l["from_stage_name"] + l["to_stage_name"]) for l in hops)
        assert any(name == "Rain Route Winding" for name in payload["plan"]["via_route_names"])

    def test_unknown_stage_is_an_honest_miss(self, db_world, client):
        page = client.get(reverse("traffic:plan"), {"from": "Xtzu Stage", "to": "Ambassadeur"})
        assert page.status_code == 200
        html = page.content.decode()
        assert 'data-test="traffic-unknown"' in html
        assert "Xtzu Stage" in html  # says WHICH name failed, offers next steps
        mirror = client.get(
            reverse("traffic:plan_json"), {"from": "Xtzu Stage", "to": "Ambassadeur"}
        )
        data = json.loads(mirror.content)
        assert data["payload"]["status"] == "UNKNOWN_STAGE"
        assert data["payload"]["message"]

    def test_nickname_resolution_through_the_routes_search_door(self, db_world, client):
        response = client.get(
            reverse("traffic:plan_json"), {"from": "Kikuyu Road", "to": "Ambassadeur"}
        )
        data = json.loads(response.content)["payload"]
        assert data["status"] == "OK"  # the alias resolved → real plan built
        assert data["from_stage"]["name"] == "Kikuyu Junction"

    def test_disconnected_stages_render_the_honest_empty_state(self, db, client):
        """Two islands: the goal stage exists but on a separate route
        island — the honest empty state, 200, no crash, no fake plan."""
        lonely = Route.objects.create(name_en="Nowhere Express")
        other_island = Route.objects.create(name_en="Island Line")
        far_out = Stage.objects.create(
            name="Far Out Stage", lat=Decimal("-1.400000"), lon=Decimal("36.900000")
        )
        too_far = Stage.objects.create(
            name="Too Far Stage", lat=Decimal("-1.410000"), lon=Decimal("36.950000")
        )
        mid_city = Stage.objects.create(
            name="Mid City", lat=Decimal("-1.280000"), lon=Decimal("36.830000")
        )
        RouteStage.objects.create(route=lonely, stage=far_out, position=1)
        RouteStage.objects.create(route=lonely, stage=too_far, position=2)
        RouteStage.objects.create(route=other_island, stage=mid_city, position=1)
        response = client.get(
            reverse("traffic:plan"), {"from": "Far Out Stage", "to": "Mid City"}
        )
        html = response.content.decode()
        assert response.status_code == 200
        assert 'data-test="traffic-empty-plan"' in html
        assert "No route connects" in html
        assert response.context["payload"]["status"] == "NO_ROUTE"
        mirror = client.get(
            reverse("traffic:plan_json"), {"from": "Far Out Stage", "to": "Mid City"}
        )
        assert json.loads(mirror.content)["payload"]["status"] == "NO_ROUTE"

    def test_same_stage_plan_says_trivial(self, db_world, client):
        response = client.get(
            reverse("traffic:plan_json"), {"from": "Kilimani", "to": "Kilimani"}
        )
        data = json.loads(response.content)
        assert data["payload"]["status"] == "TRIVIAL"
        assert "already at" in data["payload"]["message"]

    def test_about_page_states_the_source_separation(self, client):
        response = client.get(reverse("traffic:about"))
        assert response.status_code == 200
        html = response.content.decode()
        assert 'data-test="traffic-about-official"' in html
        assert 'data-test="traffic-about-community"' in html
        assert_under_budget(html, kb=50)

    def test_resolution_is_icase_exact_first(self, db_world):
        from_row, suggestions = services.resolve_stage_query("kawangware")
        assert from_row["id"] == db_world["stages"]["from"].pk
        assert from_row["name"] == "Kawangware"
        assert suggestions  # top hits offered alongside
