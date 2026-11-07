"""retrieval.py — DETERMINISTIC keyword retrieval over the guides corpus
(PROMPTS.md Prompt 34: substitutions grounded in the guides corpus 08).

PURE MODULE: no Django imports, no network, no embeddings. build_index()
and retrieve() are plain functions over dicts, so the same corpus + query
return identical top-k on every call (tested twice in a row in
tests/test_assistant.py). Scoring is token overlap between the query and
each row's title/keywords/tags tokens plus a fixed bonus when a query token
matches the row's own category vocabulary — NO embedding calls anywhere
(data-budget hard rule: retrieval computes locally, it never dials out).

INPUT CONTRACT — the "corpus row" dicts the Django layer builds (see
apps.ai.assistant.corpus_rows, fed from the guides PUBLIC read door):

    {"pk": int, "title": str, "category": str,
     "safety_category": str, "safety_warning": str,
     "keywords": str, "tags": str}

``pk``/``title``/``category`` are required; the rest default safely. Extra
keys ride along untouched (retrieve() copies the row's category out —
callers reach the full row via the index: ``index["rows"][pk]``).
"""
from __future__ import annotations

import re

#: Lowercase alnum runs — plain, deterministic tokenization (never the
#: salted builtin hash(), never punctuation-sensitive splits).
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Intake boilerplate — present in every member's words, in no guide's
#: title text. Structural words only: content words (sufuria, ugali, jiko…)
#: must keep scoring weight.
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "on",
    "at", "is", "it", "i", "my", "me", "you", "your", "what", "how", "can",
    "if", "that", "this", "do", "does", "did", "be", "are", "will", "was",
    "have", "has", "had", "need", "neednt", "want", "there", "make",
    "use", "used", "so", "from", "into", "about", "get",
})

#: Fixed bonus when a query token matches the row's category tokens —
#: two rows with equal overlap are ordered by their category relevance.
CATEGORY_MATCH_BONUS = 1.0


def tokenize(text) -> tuple[str, ...]:
    """Deterministic tokens: lowercase alnum runs, stopwords dropped,
    first-seen order (dedup). Anything non-str is str()'d — honest input."""
    seen: dict[str, None] = {}
    for word in _TOKEN_RE.findall(str(text or "").lower()):
        if word not in STOPWORDS:
            seen[word] = None
    return tuple(seen)


def _as_terms(value) -> frozenset[str]:
    """A string (or list/tuple/set of strings) → frozenset of tokens."""
    if isinstance(value, (list, tuple, set, frozenset)):
        value = " ".join(str(part) for part in value)
    return frozenset(tokenize(value))


def build_index(corpus_rows: list[dict]) -> dict:
    """Build the retrieval index over the given corpus rows (pure dicts).

    Shape (all plain dicts/tuples — a fixture-friendly structure):
      {"rows":            {pk: row},            # untouched corpus rows
       "inverted":        {term: (pk, …)},      # term → pk bucket (sorted)
       "category_tokens": {pk: frozenset(str)}} # the row's category words

    A duplicate ``pk`` raises loudly — a corpus may never double-count a
    guide (every citation would be ambiguous).
    """
    rows: dict[int, dict] = {}
    inverted: dict[str, list[int]] = {}
    category_tokens: dict[int, frozenset] = {}
    for row in corpus_rows:
        pk = int(row["pk"])
        if pk in rows:
            raise ValueError(f"corpus row pk {pk} appears twice — never cite an ambiguous guide")
        rows[pk] = row
        title_terms = _as_terms(row.get("title"))
        kw_terms = _as_terms(row.get("keywords")) | _as_terms(row.get("tags"))
        for term in sorted(title_terms | kw_terms):
            inverted.setdefault(term, [])
            if pk not in inverted[term]:
                inverted[term].append(pk)
        category_tokens[pk] = _as_terms(row.get("category"))
    return {
        "rows": rows,
        # tuple(sorted(...)) → deterministic buckets regardless of input order
        "inverted": {term: tuple(sorted(pks)) for term, pks in inverted.items()},
        "category_tokens": category_tokens,
    }


def retrieve(index: dict, query_text: str, *, limit: int = 3) -> list[tuple[int, float, str]]:
    """Deterministic ranked hits: ``[(guide_pk, score, category), …]``.

    Scoring (deterministic only — no embeddings, no network):
    +1.0 per query token present in the row's indexed terms, plus
    CATEGORY_MATCH_BONUS once when any query token matches the row's
    category tokens. Only positive scores rank; ties break on the SMALLER
    guide_pk; the list is truncated at ``limit`` hits.

    Same corpus + same query → the identical list, byte-stable (the
    accumulation order is fixed by iterating sorted query tokens).
    """
    query_terms = frozenset(tokenize(query_text))
    if not query_terms:
        return []
    seen_scores: dict[int, float] = {}
    for term in sorted(query_terms):  # fixed accumulation order
        for pk in index["inverted"].get(term, ()):
            seen_scores[pk] = seen_scores.get(pk, 0.0) + 1.0
    ranked: list[tuple[int, float, str]] = []
    for pk in sorted(seen_scores):
        score = seen_scores[pk]
        if query_terms & index["category_tokens"].get(pk, frozenset()):
            score += CATEGORY_MATCH_BONUS
        ranked.append((pk, round(score, 4), index["rows"][pk]["category"]))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    if limit is None:
        return ranked
    return ranked[: max(0, limit)]
