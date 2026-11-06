"""search.py — nickname/alias trigram search over the route network.

PROMPTS.md 25: "search by nickname: stage/route alias table with trigram
matching". Same discipline as apps/search/views.py (Prompt 14):

- raw SQL over OUR OWN tables (no cross-app internals);
- pg_trgm `%` + `<%` (word_similarity) operators — the only forms the GIN
  indexes created in routes 0002 can serve;
- thresholds pinned with SET LOCAL so the tolerance is exactly SIMILARITY_
  THRESHOLD and both queries (rows + nothing else) share one snapshot;
- LIMIT on rendered rows (≤20 per list surface — hard rule);
- deterministic order: score DESC, then id, so pages never reshuffle.

NOT fabricated: an empty or no-match query returns the honest empty state.
"""
from __future__ import annotations

from django.db import connection, transaction

SIMILARITY_THRESHOLD = 0.35  # same floor the site-wide search verifies
LIMIT = 20

# Routes match on: en/sw name, SACCO name, and ANY stage on the route
# (stage name OR alias — "destination" queries hit the corridor's terminal
# stage through the same predicate).
_ROUTES_SQL = (
    """
    SELECT r.id, r.name_en, r.name_sw, r.corridor_color, r.shape_source,
           r.seed_origin,
           GREATEST(
             similarity(r.name_en, %(q)s),
             similarity(r.name_sw, %(q)s),
             word_similarity(%(q)s, r.name_en),
             word_similarity(%(q)s, r.name_sw)
           ) AS title_score,
           EXISTS (
             SELECT 1
             FROM routes_route_saccos rs
             JOIN routes_sacco s ON s.id = rs.sacco_id
             WHERE rs.route_id = r.id
               AND (s.name %% %(q)s OR %(q)s <%% s.name)
           ) AS sacco_hit,
           EXISTS (
             SELECT 1
             FROM routes_routestage rl
             JOIN routes_stage st ON st.id = rl.stage_id
             LEFT JOIN routes_stagealias sa ON sa.stage_id = st.id
             WHERE rl.route_id = r.id
               AND (st.name %% %(q)s OR %(q)s <%% st.name
                    OR sa.alias %% %(q)s OR %(q)s <%% sa.alias)
           ) AS stage_hit
    FROM routes_route r
    WHERE r.name_en %% %(q)s
       OR r.name_sw %% %(q)s
       OR %(q)s <%% r.name_en
       OR %(q)s <%% r.name_sw
       OR EXISTS (SELECT 1
                  FROM routes_route_saccos rs JOIN routes_sacco s ON s.id = rs.sacco_id
                  WHERE rs.route_id = r.id
                    AND (s.name %% %(q)s OR %(q)s <%% s.name))
       OR EXISTS (SELECT 1
                  FROM routes_routestage rl
                  JOIN routes_stage st ON st.id = rl.stage_id
                  LEFT JOIN routes_stagealias sa ON sa.stage_id = st.id
                  WHERE rl.route_id = r.id
                    AND (st.name %% %(q)s OR %(q)s <%% st.name
                         OR sa.alias %% %(q)s OR %(q)s <%% sa.alias))
    ORDER BY title_score DESC, sacco_hit DESC, stage_hit DESC, r.id ASC
    LIMIT %(limit)s
    """
)

# Stages match on: the stage's own name and its alias table.
_STAGES_SQL = (
    """
    SELECT st.id, st.name, st.lat, st.lon, st.seed_origin, st.continuous_stop,
           GREATEST(
             similarity(st.name, %(q)s),
             word_similarity(%(q)s, st.name),
             COALESCE(MAX(similarity(sa.alias, %(q)s)), 0),
             COALESCE(MAX(word_similarity(%(q)s, sa.alias)), 0)
           ) AS title_score
    FROM routes_stage st
    LEFT JOIN routes_stagealias sa ON sa.stage_id = st.id
    WHERE st.name %% %(q)s
       OR %(q)s <%% st.name
       OR sa.alias %% %(q)s
       OR %(q)s <%% sa.alias
    GROUP BY st.id
    ORDER BY title_score DESC, st.id ASC
    LIMIT %(limit)s
    """
)


def search_network(q: str):
    """Return (route_rows, stage_rows) matching nickname/name/sacco/segment.

    Rows are plain dicts in deterministic rank order; callers render them
    directly. ``%%`` is psycopg3's escape for a literal % in parametrised SQL.
    """
    q = (q or "").strip()
    if not q:
        return [], []
    params = {"q": q, "limit": LIMIT}
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_THRESHOLD}")
            cursor.execute(
                f"SET LOCAL pg_trgm.word_similarity_threshold = {SIMILARITY_THRESHOLD}"
            )
            cursor.execute(_ROUTES_SQL, params)
            rcols = [c[0] for c in cursor.description]
            routes = [dict(zip(rcols, row)) for row in cursor.fetchall()]
            cursor.execute(_STAGES_SQL, params)
            scols = [c[0] for c in cursor.description]
            stages = [dict(zip(scols, row)) for row in cursor.fetchall()]
    return routes, stages
