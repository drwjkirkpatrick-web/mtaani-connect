"""graph.py — the PURE journey-planner core (Prompt 32). No Django imports.

The Prompt-11/30 pure pattern (feed/ranking.py, crowd/weighting.py): the
algorithm is plain Python over plain dicts — the CALLER (services.py) has
already pulled every row through each app's public door, so this module
never touches settings, the ORM or a clock. That keeps Dijkstra
unit-testable against hand-built fixtures with no database, and makes the
whole data flow auditable: rows in → plan out.

WHAT THIS MODULE OWNS (the planner contract, also consumed by Prompt 34's
"Ask Mtaani" route answers and a future cron):

    Graph         build_graph(stagelink_rows, fare_rows, crowd_rows=None) -> Graph
    plan          graph.plan(graph, start_stage_id, goal_stage_id) -> dict | None
                      {"legs": [...], "total_minutes": float,
                       "usual": {...} | None, "avoiding": {...} | None}
                  None ⇔ no route connects start and goal (never ∞, never
                  a crash — the caller shows the honest empty plan).
    dijkstra      graph.dijkstra(graph, start, goal) -> movement legs | None
    walk_legs     walk_legs(stage_rows) -> walk pairs within WALK_METERS
    fares         aggregate_fares(legs) -> (min_total, max_total, key)

STAGE GRAPH (spec verbatim: pure Python, no routing engines — the network
is a few hundred stages). Nodes are (stage_id, route_id or None).
Edge families:

  * stage-hop   consecutive stagelinks on one route, BOTH directions.
                Minutes = WALK_TIME_PER_KM_MIN × haversine distance of the
                two stage coordinates; when a stage has no coordinates the
                hop is honest DEFAULT_EDGE_MIN and flagged ``estimated``.
  * walk        stages within WALK_METERS of each other (walk_legs()).
                Only available from a matatu state or on foot — you get
                OFF, walk, and are now on foot at the arrival stage.
  * transfer    route swap at a SHARED stage: TRANSFER_PENALTY_MIN wait
                (the same wait you would stand through).
  * board       (stage, None) → (stage, r): free — starting a journey or
                stepping on after walking; silent (never renders as a leg).

WHY boarding is free but transfers are not: boarding the first matatu of a
journey happens once you're already standing at the stage; a transfer is a
SECOND wait between vehicles. This makes "with vs without penalty" differ
by exactly one TRANSFER_PENALTY_MIN on a one-transfer plan (test-pinned),
and penalty=0 collapses to the pure graph sum of minutes.
The same-stage route swap also cannot be dodged with a 0-minute detour:
there is no "alight to on-foot at the same stage" edge — the only ways out
of a matatu state are a real walk (≥ walk minutes) or a real transfer.

DISRUPTION ANNOTATION (Prompt 30 crowd rows, overlay-shaped dicts):
    DELAY         +N minutes        (N = merged_count — riders who said so)
    FLOODING      +2N minutes
    STAGE_CLOSED  the edge cannot be entered → planner picks another route
Every other kind is honest silence: no invented delay, non-blocking. The
weights table below is the ONLY tuning point, module-level and pure.

CITATIONS: each annotated leg carries disruption rows that quote the
crowd payload back verbatim (count, first_seen, age, note — optional
report_id), source "community" + Confidence COMMUNITY_REPORT. Nothing is
invented here; sentences are composed by views/services (i18n seam).

SOURCE SEPARATION per leg: "official" (admin-curated route) or "seed"
(2018 GTFS seed) describes the ROUTE data; community conditions ride on
top as the cited disruptions. A template can colour the two apart using
exactly these keys.

FARES (Prompt 25): legs resolve RANGES — the exact segment Fare row, then
the reversed pair, then the route's overall observed range, else None
(unknown is displayed, never guessed). aggregate_fares() sums the leg
bounds; a plan NEVER collapses to one official KSh number.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Iterable

from apps.core.geo import distance_km

# ------------------------------------------------------------------ tunes
# WHY one pace constant for hops and walks (spec verbatim: hop minutes =
# WALK_TIME_PER_KM_MIN × distance): the routes DB has no segment speeds, so
# a single conservative city pace beats an invented per-route table.
# Retuning is a constants change, never hidden magic.
WALK_TIME_PER_KM_MIN = 12.0  # ≈ 5 km/h across the city — realistic walking
DEFAULT_EDGE_MIN = 4.0       # hop fallback when a stage has no coordinates
TRANSFER_PENALTY_MIN = 5.0   # the wait you stand through when swapping routes
WALK_METERS = 300.0          # cap for a "just walk over" inter-stage hop
# Degree cell ~300 m at Nairobi latitudes (1° ≈ 111 km): the bucket grid
# turn walk_legs from O(n²) to a few neighbour-cell checks per stage.
_WALK_CELL_DEG = 0.0027

# The disruption-delay table — per-riders-said-so minutes (module-level,
# pure, the only tuning point; KISS: only kinds the spec names have an
# effect — every other kind costs nothing rather than an invented figure).
DISRUPTION_DELAY_MINUTES = {
    "DELAY": 1.0,     # +N minutes
    "FLOODING": 2.0,  # +2N minutes
}
DISRUPTION_BLOCKING_KINDS = ("STAGE_CLOSED",)  # edges through it are dropped

LEG_KINDS = ("stage-hop", "walk", "transfer")  # kinds that render as legs
SILENT_KINDS = ("board",)  # 0-minute state moves: never render
LEG_SOURCE_OFFICIAL = "official"
LEG_SOURCE_SEED = "seed"
DISRUPTION_SOURCE = "community"  # every crowd condition line is community

# Fare-aggregation confidence vocabulary (machine keys; views translate):
FARE_OBSERVED = "OBSERVED_RANGES"   # every paid leg has an observed range
FARE_PARTIAL = "PARTIAL_FARES"      # some paid legs lack observed fares
FARE_NO_PAID_LEGS = "NO_PAID_LEGS"  # nothing paid (e.g. walk-only plan)

Node = tuple[int, int | None]

# Row contracts (caller-shaped, documented at build_graph / walk_legs).


@dataclass
class Graph:
    """The built, annotated network — pure data, no behaviour of its own."""

    adjacency: dict[Node, list[dict]] = field(default_factory=dict)
    stages: dict[int, dict] = field(default_factory=dict)  # id → name/lat/lon
    routes: dict[int, dict] = field(default_factory=dict)  # id → name/seed
    crowd_row_count: int = 0  # how many community rows were applied (cited)


# ------------------------------------------------------------------- build
def _hop_minutes(a_coord, b_coord) -> tuple[float, bool, float | None]:
    """(minutes, estimated, distance_km) between two stage coordinate pairs."""
    if a_coord is None or b_coord is None:
        return round(DEFAULT_EDGE_MIN, 1), True, None  # honest fallback, flagged
    km = round(distance_km(a_coord, b_coord), 3)
    return round(km * WALK_TIME_PER_KM_MIN, 1), False, km


def _fare_for(fares_by_route: dict, route_id: int, a: int, b: int):
    """The observed fare RANGE for one leg (never a single number).

    Tiers: exact (route, from, to) → reversed pair (real matatus charge the
    same both ways and rows exist per direction) → the route's overall
    observed range → None. Return {lo, max, confidence, basis} | None.
    """
    rows = fares_by_route.get(route_id) or []
    exact = next((f for f in rows if f["from_stage_id"] == a and f["to_stage_id"] == b), None)
    if exact is not None:
        basis = "segment"
    else:
        exact = next((f for f in rows if f["from_stage_id"] == b and f["to_stage_id"] == a), None)
        basis = "segment_reverse" if exact is not None else None
    if exact is not None:
        return {
            "lo": int(exact["min_price"]),
            "max": int(exact["max_price"]),
            "confidence": str(exact["confidence"]),
            "basis": basis,
        }
    if rows:  # route-level default: the route's overall observed range
        widest = max(rows, key=lambda f: (f["max_price"], -f["pk"]))
        return {
            "lo": min(int(f["min_price"]) for f in rows),
            "max": int(widest["max_price"]),
            "confidence": str(widest["confidence"]),
            "basis": "route",
        }
    return None  # unknown is honest — the caller displays it as such


def _build_hop_edges(stagelink_rows: Iterable[dict]) -> tuple[list[dict], dict[int, list[int]]]:
    """Consecutive stagelinks (per route, position order) → hop edges.

    Returns (edges, routes_at) — routes_at maps stage_id → serving route
    ids for the walk-edge family. Each hop is inserted both directions
    (matatus run back the same corridor; fares resolve per direction).
    """
    routes_at: dict[int, set[int]] = {}
    current_route = None
    stage_meta = None  # (stage_id, name, coord) of the previous row
    hops: list[dict] = []
    for row in stagelink_rows:
        route_id = int(row["route_id"])
        if route_id != current_route:
            current_route = route_id
            stage_meta = None  # positions restart per route
        route_meta = int(route_id), row["route_name"], bool(row.get("route_seed", True))
        stage_id = int(row["stage_id"])
        coord = None
        if row.get("lat") is not None and row.get("lon") is not None:
            coord = (float(row["lat"]), float(row["lon"]))
        if stage_meta is not None:
            prev_id, prev_name, prev_coord = stage_meta
            minutes, estimated, hop_km = _hop_minutes(prev_coord, coord)
            hop = {
                "kind": "stage-hop",
                "route_id": route_id,
                "route_name": route_meta[1],
                "source": LEG_SOURCE_SEED if route_meta[2] else LEG_SOURCE_OFFICIAL,
                "from_stage": prev_id,
                "to_stage": stage_id,
                "from_stage_name": prev_name,
                "to_stage_name": row["stage_name"],
                "minutes": minutes,
                "delay_minutes": 0.0,
                "estimated": estimated,
                "distance_km": hop_km,
                "fare": None,
                "disruptions": [],
            }
            hops.append(hop)
        routes_at.setdefault(stage_id, set()).add(route_id)
        stage_meta = (stage_id, row["stage_name"], coord)
    return hops, {k: sorted(set(v)) for k, v in routes_at.items()}


def walk_legs(stage_rows: Iterable[dict], walk_meters: float = WALK_METERS) -> list[dict]:
    """Stages within ``walk_meters`` of each other (PURE, grid-bucketed).

    Input rows: {"stage_id", "lat", "lon"} (lat/lon may be None). Returns
    walk-pair dicts {"from_stage", "to_stage", "minutes", "distance_km"}
    — the caller (build_graph) turns them into labeled WALK edges.
    WHY a grid: a one-degree cell is far more than walk_meters at Nairobi
    latitudes, so pairs can only sit in ≥-adjacent cells — a 9-cell scan
    per stage replaces an O(n²) haversine sweep of every pair.
    """
    cell = _WALK_CELL_DEG
    buckets: dict[tuple[int, int], list[tuple[int, tuple[float, float]]]] = {}
    for row in stage_rows:
        if row.get("lat") is None or row.get("lon") is None:
            continue  # no coordinates → no honest walk length; skip silently
        lat, lon = float(row["lat"]), float(row["lon"])
        key = (int(lat // cell), int(lon // cell))
        buckets.setdefault(key, []).append((int(row["stage_id"]), (lat, lon)))
    pairs: dict[tuple[int, int], dict] = {}
    for (bx, by), items in buckets.items():
        neighbours = [
            b for b in buckets if bx - 1 <= b[0] <= bx + 1 and by - 1 <= b[1] <= by + 1
        ]
        for nb in neighbours:
            others = buckets[nb]
            for this_id, this_coord in items:
                for other_id, other_coord in others:
                    if this_id == other_id:
                        continue
                    key = (min(this_id, other_id), max(this_id, other_id))
                    if key in pairs:
                        continue
                    meters = distance_km(this_coord, other_coord) * 1000.0
                    if meters > walk_meters or meters <= 0.0:
                        continue
                    km = meters / 1000.0
                    pairs[key] = {
                        "from_stage": this_id,
                        "to_stage": other_id,
                        "minutes": round(km * WALK_TIME_PER_KM_MIN, 1),
                        "distance_km": round(km, 3),
                    }
    return sorted(pairs.values(), key=lambda p: (p["from_stage"], p["to_stage"]))


def build_graph(
    stagelink_rows: Iterable[dict],
    fare_rows: Iterable[dict],
    crowd_rows: Iterable[dict] | None = None,
    *,
    walk_meters: float = WALK_METERS,
) -> Graph:
    """The network built ONCE per request — spec: pure, no routing engine.

    Row contracts:
      stagelink_row: {"route_id","route_name","route_seed","stage_id",
                      "stage_name","lat","lon","position"} ordered by
                      (route_id, position);
      fare_row:      {"route_id","from_stage_id","to_stage_id","min_price",
                      "max_price","confidence","pk"};
      crowd_row:     the crowd payload's vocabulary {"stage_id","type",
                      "count","age_min","first_seen_at","note"} (overlay
                      shape — citations ride back to the UI verbatim) plus
                      an optional "report_id" passthrough.
    """
    links = list(stagelink_rows)
    hops, routes_at = _build_hop_edges(links)
    graph = Graph()
    for row in links:
        stage_id = int(row["stage_id"])
        graph.stages[stage_id] = {
            "name": row["stage_name"],
            "lat": row.get("lat"),
            "lon": row.get("lon"),
        }
        graph.routes[int(row["route_id"])] = {
            "name": row["route_name"],
            "seed": bool(row.get("route_seed", True)),
        }

    fares_by_route: dict[int, list[dict]] = {}
    for fare in fare_rows:
        fares_by_route.setdefault(int(fare["route_id"]), []).append(fare)

    adjacency: dict[Node, list[dict]] = {}
    for hop in hops:
        hop["fare"] = _fare_for(
            fares_by_route, int(hop["route_id"]), int(hop["from_stage"]), int(hop["to_stage"])
        )
        a = (hop["from_stage"], hop["route_id"])
        b = (hop["to_stage"], hop["route_id"])
        adjacency.setdefault(a, []).append(hop)
        # NOTE: the reversed hop is a SEPARATE edge whose fare must be
        # resolved for ITS direction — a dict copy would inherit the
        # original's fare (the segment-reverse tier needs its own lookup).
        reverse = dict(hop, from_stage=hop["to_stage"], to_stage=hop["from_stage"],
                       from_stage_name=hop["to_stage_name"], to_stage_name=hop["from_stage_name"])
        reverse["fare"] = _fare_for(
            fares_by_route, int(hop["route_id"]), int(hop["to_stage"]), int(hop["from_stage"])
        )
        adjacency.setdefault(b, []).append(reverse)

    # walking: pair up nearby stages, then expose the walk from any route
    # serving the departure stage AND from bare on-foot states. Walk edges
    # are labelled by real distance (never "estimated").
    stages = [{"stage_id": sid, "lat": row["lat"], "lon": row["lon"]}
              for sid, row in graph.stages.items()]
    for pair in walk_legs(stages, walk_meters=walk_meters):
        a_id, b_id = pair["from_stage"], pair["to_stage"]
        minutes = pair["minutes"]
        # NOTE: a walk is available on foot (None state — journey start or
        # after stepping off) AND from any route serving the departure
        # stage; it ALWAYS lands on foot at the arrival stage (b, None).
        for depart, arrive in ((a_id, b_id), (b_id, a_id)):
            for depart_route in [None, *routes_at.get(depart, [])]:
                edge = {
                    "kind": "walk",
                    "route_id": None,
                    "route_name": None,
                    "source": LEG_SOURCE_OFFICIAL,  # distance = routes-DB coords
                    "from_stage": depart,
                    "to_stage": arrive,
                    "from_stage_name": graph.stages[depart]["name"],
                    "to_stage_name": graph.stages[arrive]["name"],
                    "minutes": minutes,
                    "delay_minutes": 0.0,
                    "estimated": False,
                    "distance_km": pair["distance_km"],
                    "fare": None,  # walking is free
                    "disruptions": [],
                }
                adjacency.setdefault((depart, depart_route), []).append(edge)

    # transfers at shared stages (both directions, cost = the penalty).
    for stage_id, route_ids in routes_at.items():
        for i, ra in enumerate(route_ids):
            for rb in route_ids[i + 1:]:
                for ra_i, rb_i in ((ra, rb), (rb, ra)):
                    penalty = {
                        "kind": "transfer",
                        "route_id": rb_i,
                        "route_name": graph.routes[rb_i]["name"],
                        "source": (
                            LEG_SOURCE_SEED
                            if graph.routes[rb_i]["seed"] else LEG_SOURCE_OFFICIAL
                        ),
                        "from_stage": stage_id,
                        "to_stage": stage_id,
                        "from_stage_name": graph.stages[stage_id]["name"],
                        "to_stage_name": graph.stages[stage_id]["name"],
                        "minutes": round(TRANSFER_PENALTY_MIN, 1),
                        "delay_minutes": 0.0,
                        "estimated": False,
                        "distance_km": None,
                        "fare": None,
                        "disruptions": [],
                    }
                    adjacency.setdefault((stage_id, ra_i), []).append(penalty)
                    # the reverse edge is rebuilt on the other route's turn

        # boards: on-foot → any serving route (free; starting/stepping on)
    for stage_id, route_ids in routes_at.items():
        for route_id in route_ids:
            board_edge = {
                "kind": "board",
                "route_id": route_id,
                "route_name": graph.routes[route_id]["name"],
                "source": (
                    LEG_SOURCE_SEED if graph.routes[route_id]["seed"]
                    else LEG_SOURCE_OFFICIAL
                ),
                "from_stage": stage_id,
                "to_stage": stage_id,
                "from_stage_name": graph.stages[stage_id]["name"],
                "to_stage_name": graph.stages[stage_id]["name"],
                "minutes": 0.0,
                "delay_minutes": 0.0,
                "estimated": False,
                "distance_km": None,
                "fare": None,
                "disruptions": [],
            }
            adjacency.setdefault((stage_id, None), []).append(board_edge)

    # crowd annotation pass (community conditions riding ON the official):
    # each annotation applies ONCE per edge it touches (stage at either
    # end), stacking different kinds. A BLOCKED kind keeps the edge in the
    # graph flagged closed (the counterfactual 'usual route' can still
    # show it, cited); the planner refuses it in apply mode — the plan
    # must go around a closed stage, or honestly report None.
    crowd_list = list(crowd_rows or [])
    for node, edges in adjacency.items():
        kept = []
        for edge in edges:
            if edge["kind"] == "stage-hop":
                blocked = False
                delay = 0.0
                cites = []
                for ann in crowd_list:
                    if int(ann.get("stage_id", -1)) not in (
                        int(edge["from_stage"]), int(edge["to_stage"])
                    ):
                        continue
                    kind = str(ann.get("type", ""))
                    if kind in DISRUPTION_BLOCKING_KINDS:
                        blocked = True
                        cites.append(_citation(ann, delay_min=0.0))
                        continue
                    multiplier = DISRUPTION_DELAY_MINUTES.get(kind, 0.0)
                    if multiplier <= 0.0:
                        continue  # honest silence: no invented delay
                    delay_min = round(int(ann.get("count", 1)) * multiplier, 1)
                    delay += delay_min
                    cites.append(_citation(ann, delay_min=delay_min))
                if cites:
                    edge = dict(
                        edge,
                        delay_minutes=round(delay, 1),
                        disruptions=cites,
                        blocked=blocked,
                    )
            elif edge["kind"] == "transfer":
                edge = dict(edge)
            kept.append(edge)
        adjacency[node] = kept
    graph.adjacency = adjacency
    graph.crowd_row_count = len(crowd_list)
    return graph


def _citation(ann: dict, *, delay_min: float) -> dict:
    """One disruption citation — ONLY keys from the crowd payload (never
    invented): what/who-count/age/first-seen, delay effect, source labels.
    """
    row = {
        "kind": str(ann.get("type", "")),
        "count": int(ann.get("count", 1)),
        "age_min": round(float(ann.get("age_min", 0.0)), 1),
        "first_seen_at": ann.get("first_seen_at", ""),
        "stage_name": str(ann.get("stage_name", "")) if ann.get("stage_name") else "",
        "delay_min": round(delay_min, 1),
        "note": ann.get("note", "") or "",
        "source": DISRUPTION_SOURCE,
        "confidence": "COMMUNITY_REPORT",
    }
    if ann.get("report_id") is not None:
        row["report_id"] = int(ann["report_id"])
    return row


def _edge_cost(edge: dict, *, transfer_penalty_min: float, crowd_mode: str) -> float:
    """Effective minutes this edge costs under a planning mode.

    crowd_mode: "apply" → hop minutes + crowd delays, CLOSED hops cost
    infinite (refused); "ignore" → pure base minutes (the counterfactual
    'usual route' — a closed stage rides at its pre-report cost, the
    citation travels with it); "avoid" → hops touched by ANY community
    report cost infinite (the steer-clear alternative). Walk legs carry
    no community conditions by design; transfers cost the penalty.
    """
    kind = edge["kind"]
    if crowd_mode == "avoid":
        if kind == "stage-hop" and (edge.get("disruptions") or edge.get("blocked")):
            return float("inf")
        crowd_mode = "ignore"  # hop without reports → base minutes
    if crowd_mode == "apply" and edge.get("blocked"):
        return float("inf")
    minutes = float(edge.get("minutes") or 0.0)
    if crowd_mode == "apply":
        minutes += float(edge.get("delay_minutes") or 0.0)
    if kind == "transfer":
        return transfer_penalty_min
    return minutes


def dijkstra(
    graph: Graph,
    start_stage_id: int,
    goal_stage_id: int,
    *,
    transfer_penalty_min: float = TRANSFER_PENALTY_MIN,
    crowd_mode: str = "apply",
) -> list[dict] | None:
    """Shortest legs start→goal, or None when nothing connects them.

    Returns movement legs in travel order ("stage-hop" / "walk" /
    "transfer"); silent "board" edges are filtered out of the display but
    still cost their (always zero) minutes. Never raises on unreachable
    pairs and never renders ∞ — disconnected → None (honest empty state).
    """
    start = (int(start_stage_id), None)
    goal_nodes = {(int(goal_stage_id), None)}
    for route_id in graph.routes:
        goal_nodes.add((int(goal_stage_id), int(route_id)))
    if start not in graph.adjacency and start_stage_id not in graph.stages:
        return None
    distances: dict[Node, float] = {start: 0.0}
    prev: dict[Node, Node] = {}
    prev_edge: dict[Node, dict] = {}
    queue: list[tuple[float, int, Node]] = [(0.0, 0, start)]
    order = 0
    while queue:
        dist, _, node = heapq.heappop(queue)
        if dist > distances.get(node, float("inf")):
            continue
        if node in goal_nodes:
            legs = []
            walk_back = node
            while walk_back != start:
                edge = prev_edge[walk_back]
                edge = dict(edge)
                if edge["kind"] == "transfer":
                    edge["minutes"] = round(transfer_penalty_min, 1)
                legs.append(edge)
                walk_back = prev[walk_back]
            legs.reverse()
            return [l for l in legs if l["kind"] in LEG_KINDS]
        for edge in graph.adjacency.get(node, []):
            cost = _edge_cost(edge, transfer_penalty_min=transfer_penalty_min, crowd_mode=crowd_mode)
            if cost >= float("inf"):
                continue
            # A walk does not carry the matatu state with it: stepping off
            # and walking always lands ON FOOT at the arrival stage.
            if edge["kind"] == "walk":
                nxt = (int(edge["to_stage"]), None)
            elif edge["kind"] == "board":
                nxt = (int(edge["to_stage"]), int(edge["route_id"]))
            else:  # stage-hop / transfer: stay on (or join) the same route
                nxt = (int(edge["to_stage"]), int(edge["route_id"]))
            new = dist + cost
            if new < distances.get(nxt, float("inf")):
                distances[nxt] = new
                prev[nxt] = node
                prev_edge[nxt] = edge
                order += 1
                heapq.heappush(queue, (new, order, nxt))
    return None


def _sequence(legs: Iterable[dict]) -> tuple:
    """Movement-leg identity — alternates are compared by THIS, not by
    minute totals (a same-route-with-same-steps alternate is noise)."""
    return tuple(
        (l["kind"], l.get("route_id"), l.get("from_stage"), l.get("to_stage"))
        for l in legs
    )


def plan(
    graph: Graph,
    start_stage_id: int,
    goal_stage_id: int,
    *,
    transfer_penalty_min: float = TRANSFER_PENALTY_MIN,
) -> dict | None:
    """Primary journey + honest alternates, or None when disconnected.

    The plan dict is the FROZEN contract:
      legs           [{'kind','route_id','route_name','source','from_stage',
                       'to_stage','from_stage_name','to_stage_name','minutes',
                       'delay_minutes','estimated','distance_km','fare',
                       'disruptions'}, ...]
      total_minutes   sum of minutes + delay_minutes over the legs
      usual           the counterfactual no-crowd plan, WHEN its route
                      differs — 'the usual plan before the reports'
      avoiding        the zero-community-exposure plan (disrupted edges
                      skipped), WHEN it exists and differs — 'alternative
                      via X'
    """
    if int(start_stage_id) == int(goal_stage_id):
        return {"legs": [], "total_minutes": 0.0, "usual": None, "avoiding": None}
    legs = dijkstra(
        graph, start_stage_id, goal_stage_id, transfer_penalty_min=transfer_penalty_min
    )
    if legs is None:
        return None
    total = round(sum(l["minutes"] + l["delay_minutes"] for l in legs), 1)
    plan_out = {"legs": legs, "total_minutes": total, "usual": None, "avoiding": None}

    usual_legs = dijkstra(
        graph, start_stage_id, goal_stage_id,
        transfer_penalty_min=transfer_penalty_min, crowd_mode="ignore",
    )
    if usual_legs is not None and _sequence(usual_legs) != _sequence(legs):
        # The usual route is the COUNTERFACTUAL: base minutes only (the
        # community delays stay cited on its legs, but its own total is
        # the pre-report travel time — the two numbers must not be mixed).
        usual = {
            "legs": usual_legs,
            "total_minutes": round(sum(l["minutes"] for l in usual_legs), 1),
        }
        plan_out["usual"] = usual

    avoid_legs = dijkstra(
        graph, start_stage_id, goal_stage_id,
        transfer_penalty_min=transfer_penalty_min, crowd_mode="avoid",
    )
    if avoid_legs is not None and _sequence(avoid_legs) != _sequence(legs):
        plan_out["avoiding"] = {
            "legs": avoid_legs,
            "total_minutes": round(sum(l["minutes"] for l in avoid_legs), 1),
        }
    return plan_out


def aggregate_fares(legs: Iterable[dict]) -> tuple[int, int, str]:
    """Sum the observed bounds per leg (PURE; never a single official KSh).

    Returns (min_total, max_total, confidence-key):
      OBSERVED_RANGES  every paid leg had a range → totals are the sums;
      PARTIAL_FARES    some paid legs lack fares (totals honest-only):
                       the caller must SAY totals are incomplete;
      NO_PAID_LEGS     nothing paid (e.g. a walk-only plan: walking is
                       free, not "KSh 0-80 official").
    Transfer legs carry nothing — changing routes itself is free.
    """
    lo = hi = 0
    paid = False
    unknown = 0
    for leg in legs:
        if leg.get("kind") == "stage-hop":
            paid = True
            fare = leg.get("fare")
            if fare is None:
                unknown += 1
                continue
            lo += int(fare["lo"])
            hi += int(fare["max"])
        # walk legs are free by nature; transfer legs charge minutes only
    if not paid:
        return (0, 0, FARE_NO_PAID_LEGS)
    if unknown:
        return (lo, hi, FARE_PARTIAL)
    return (lo, hi, FARE_OBSERVED)
