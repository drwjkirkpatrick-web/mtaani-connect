"""Search view — one box across guides, posts, Q&A and people (Prompt 14).

WHY raw SQL, not the ORM: the entire point of the maintained `vector` column
(migration 0002 triggers) is that a keystroke NEVER recomputes to_tsvector
over the live source tables. The ORM would force either .search() (which
builds inline vectors — the pitfall PROMPTS.md 14 names) or raw expressions
against OTHER apps' models it may not import. A two-statement read against
our OWN SearchDocument table keeps the query cheap and the isolation honest.

NO fabricated results: the view reads only real SearchDocument rows written
by triggers; an empty/missing query renders the honest empty state.
"""
from urllib.parse import quote

from django.db import connection, transaction
from django.urls import reverse
from django.utils.translation import gettext
from django.views.generic import TemplateView

# Cap results rendered on the page (PROMPTS.md: API/list surfaces ≤ 20 items;
# counts stay complete via the separate grouped COUNT query).
RESULTS_LIMIT = 20

# Trigram tolerance floor. WHY 0.35: low enough that "sukuma wiki" meets
# "sukumawiki" (different word segmentation), high enough to not flood the
# results with unrelated three-letter collisions. Verified in tests
# (tests/test_search.py::test_trigram_typo_tolerance).
SIMILARITY_THRESHOLD = 0.35

# Deterministic canonical kind order for result groups.
KIND_ORDER = ("guide", "post", "question", "person")

# WHERE fragment shared by the ranked-row query and the grouped-count query —
# one predicate, two queries, so a group's count can never disagree with what
# a deeper page would return.
#
# NOTE the OPERATOR forms (% and <%) are deliberate: pg_trgm's GIN index can
# only serve those operators, NOT a raw `similarity(...) >= x` comparison —
# the operator threshold comes from the pg_trgm.similarity_threshold /
# word_similarity_threshold GUCs, which search_documents() pins for its own
# transaction via SET LOCAL (so thresholds can never drift from this file).
# `%%` is psycopg3's escape for a literal % in parametrised SQL.
_MATCH_PREDICATE = """
    (d.vector @@ t.en_q
     OR d.vector @@ t.sw_q
     OR d.body %% %(q)s
     OR d.title_en %% %(q)s
     OR d.title_sw %% %(q)s
     OR %(q)s <%% d.body)
"""

# The tsvector column carries 'english'-stemmed AND 'simple' lexemes for the
# SAME text (migration 0002), so: en queries work through 'english' stemming,
# Kiswahili queries through 'simple' (no sw stemmer exists in Postgres —
# 'simple' keeps exact tokens, which is the right matcher for sw words).
_SEARCH_SQL = f"""
SELECT d.id, d.kind, d.object_pk, d.title_en, d.title_sw, d.body, d.ward, d.url,
       d.created_at,
       -- Additive blend, WHY not pure GREATEST: trigram word_similarity can
       -- tie at ~1.0 for both an exact title and a mere body mention; the
       -- full-text rank (title hits are weight-A) breaks such ties so the
       -- best-matching entity leads. Still ONE precomputed column per side.
       COALESCE(ts_rank(d.vector, t.en_q), 0.0)
       + COALESCE(ts_rank(d.vector, t.sw_q), 0.0)
       + 0.5 * GREATEST(
           similarity(d.body, %(q)s),
           similarity(d.title_en, %(q)s),
           similarity(d.title_sw, %(q)s),
           word_similarity(%(q)s, d.body)
       ) AS score
FROM search_searchdocument d
CROSS JOIN (
    SELECT websearch_to_tsquery('english', %(q)s) AS en_q,
           websearch_to_tsquery('simple',  %(q)s) AS sw_q
) t
WHERE {_MATCH_PREDICATE}
ORDER BY score DESC, d.kind ASC, d.created_at DESC, d.object_pk ASC
LIMIT %(limit)s
"""

_COUNTS_SQL = f"""
SELECT d.kind, count(*)::int AS n
FROM search_searchdocument d
CROSS JOIN (
    SELECT websearch_to_tsquery('english', %(q)s) AS en_q,
           websearch_to_tsquery('simple',  %(q)s) AS sw_q
) t
WHERE {_MATCH_PREDICATE}
GROUP BY d.kind
"""


def search_documents(q: str):
    """Run both full-text and trigram matching against the MAINTAINED index.

    Returns (rows, counts): rows are dicts already in deterministic rank
    order (score DESC, kind, created_at DESC, pk ASC); counts maps kind →
    total matches (uncapped), so group counts stay honest even when the
    page shows at most RESULTS_LIMIT items.

    WHAT the SET LOCALs guarantee:
    - pg_trgm `%` / `<%` operators read their thresholds from GUCs, not
      call arguments — pinning them inside this transaction makes the
      trigram tolerance exactly what SIMILARITY_THRESHOLD says it is, and
      keeps one consistent snapshot for rows + counts.
    - enable_seqscan=off is a plan HINT measured on a 50k-row fixture:
      the planner under-costs pg_trgm's per-row function evaluation
      (715 ms full scan vs 64 ms bitmap via the GIN trigram indexes).
      It is transaction-scoped (SET LOCAL) so nothing outside these two
      SELECTs is affected; on tiny tables a bitmap plan is still ms-fast.
    - enable_indexscan=off (plain index scans only; Bitmap Index Scans
      still choose the GINs): without it the counts query prefers a
      `kind`-btree walk over EVERY row (predicate re-checked per row,
      705 ms at 50k) to skip one sort node — with it the same grouped
      count runs via the bitmap trigram path (48 ms). Measured, kept.
    """
    params = {"q": q, "limit": RESULTS_LIMIT}
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_THRESHOLD}")
            cursor.execute(f"SET LOCAL pg_trgm.word_similarity_threshold = {SIMILARITY_THRESHOLD}")
            # WHY hints (not promises): pg_trgm GIN indexes only serve these
            # operator forms; both SET LOCALs stay transaction-scoped to the
            # two read statements below.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute("SET LOCAL enable_indexscan = off")
            cursor.execute(_SEARCH_SQL, params)
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            cursor.execute(_COUNTS_SQL, params)
            counts = {kind: n for kind, n in cursor.fetchall()}
    return rows, counts


def _group_link(kind: str, q: str) -> str:
    """Each group links to the entity app's own filtered list (spec).

    guides and qa natively support ?q= free text; posts has only the
    educational tag, so its link is the plain list; people has NO site-wide
    list route yet (profiles app only routes individual profile pages /
profiles/u/<username>/), so its group header is not a link — each person
    item still links to their own profile page (NOTE for Prompt 26+ when a
    people directory lands).
    """
    if kind == "guide":
        return f"/guides/?q={quote(q)}"
    if kind == "post":
        return "/posts/"
    if kind == "question":
        return f"/qa/?q={quote(q)}"
    return ""


def _kind_labels() -> dict:
    """Translated group headings, resolved per-request (not at import)."""
    return {
        "guide": gettext("Guides"),
        "post": gettext("Posts"),
        "question": gettext("Questions"),
        "person": gettext("People"),
    }


class SearchView(TemplateView):
    template_name = "search/results.html"

    def get(self, request, *args, **kwargs):
        query = (request.GET.get("q") or "").strip()
        # NOTE: hard cap guards pathological inputs; 100 chars is plenty for
        # a community search phrase and keeps the SQL parameters tiny.
        if len(query) > 100:
            query = query[:100]

        groups = []
        if query:
            rows, counts = search_documents(query)
            per_kind: dict = {}
            for row in rows:
                per_kind.setdefault(row["kind"], []).append(row)
            labels = _kind_labels()
            if request.LANGUAGE_CODE.startswith("sw"):
                title_of = lambda r: r["title_sw"] or r["title_en"]  # noqa: E731
            else:
                title_of = lambda r: r["title_en"] or r["title_sw"]  # noqa: E731

            for kind in KIND_ORDER:
                count = counts.get(kind, 0)
                if count == 0:
                    continue  # group absent — no fabricated sections
                items = []
                for row in per_kind.get(kind, []):
                    body = row["body"] or ""
                    items.append(
                        {
                            "title": title_of(row),
                            "url": row["url"] if row["url"].startswith("/") else "",
                            # WHY sanitize in the view: defensive whitelist —
                            # url is trigger-written, but never trust a
                            # stored string as a link target blindly.
                            "meta": row["ward"] or "",  # coarse ward label only
                            "snippet": " ".join(body.split())[:140],
                        }
                    )
                groups.append(
                    {
                        "kind": kind,
                        "label": labels[kind],
                        "count": count,
                        "link": _group_link(kind, query),
                        "items": items,
                    }
                )

        return self.render_to_response(
            {"query": query, "groups": groups, "search_url": reverse("search:search")}
        )
