"""Utility feed ranking (Prompt 11) — a PURE function, deliberately not a
Django module (no settings, no ORM, no timezone.now()).

WHY a pure function: PROMPTS.md's pitfall — ranking functions with now()
inside them are untestable — is defeated by taking the clock as a
parameter. `rank()` below NEVER reads the clock itself; the caller injects
`now`. Today `now` carries zero score weight (recency is not a scoring
signal, see the table), but it stays in the signature so any future
time-based adjustment (decay, freshness clamp) has an injectable home and
cannot silently grow a timezone.now() call — the determinism tests pin that
down.

SCORING TABLE (the feed's whole ordering policy — any ordering out of
`rank()` must be explainable from this table, PROMPTS.md acceptance)
=====================================================================
Signal                Points   Fact checked (where it comes from)
--------------------- -------- -----------------------------------------------
Followed author       +2.0     row["author_id"] in
                               context["followed_user_ids"] (follows PUBLIC fn)
Same ward (local)     +2.0     viewer's ward (context["viewer_ward"], via the
                               profiles PUBLIC fn) is non-empty AND equals
                               row["author_ward"] (casefolded, whitespace-
                               trimmed). No ward on either side → no boost.
Usefulness            +0..2.0  USEFULNESS_SCALE × usefulness_scorer(row).
                               THE INJECTED PLUG: Prompt 16 (trust, same wave)
                               will supply the public helpful-vote scorer —
                               the feed must NOT import it yet (mock-first),
                               so the default is `zero_usefulness` (0.0).
Guide/Q&A-linked      +1.0     row["ref_guide_id"] is not None (posts'
                               typed guide reference today; Q&A linkage
                               rides the same signal once rows carry it).
Educational tag       +1.0     row["educational"] is True (utility content —
                               brief §3 Module 4: this feed favours what
                               teaches or saves money, not what enrages).
Group overlap         +1.0     RESERVED: row["author_group_ids"] ∩
                               context["viewer_group_ids"] (groups PUBLIC fn
                               member_group_ids). Inert today — posts are not
                               group-scoped yet (group_tier_posts is an honest
                               []) — but the plumbing is live and tested.
Recency                0.0     NEVER a score, NEVER the primary sort. Applied
                               ONLY as the tie-break inside an equal score
                               (row["created_at"] DESC, then pk). A useful
                               local post from yesterday beats a fresh
                               unrelated rant: that is this table, not a bug.
=====================================================================
Theoretical max = 2.0 + 2.0 + 2.0 + 1.0 + 1.0 + 1.0 = 9.0.

KDPA note: the local signal is author ward vs viewer ward — ward is a
coarse text label (no coordinates anywhere), and the UI may show a
"Local"/"Following" badge, never the author's ward value to other members.
"""
from __future__ import annotations

from typing import Any, Callable

# WHY module constants: the docstring table and the code are one truth —
# tests assert scores THROUGH these weights, and retuning the feed means
# editing this table + constants together, never hidden magic numbers.
W_FOLLOWED = 2.0
W_LOCAL = 2.0
W_LINKED = 1.0
W_EDUCATIONAL = 1.0
W_GROUP = 1.0
USEFULNESS_SCALE = 2.0  # scorer returns [0..1]; the tier is worth at most +2

RowScorer = Callable[[dict[str, Any]], float]


def zero_usefulness(row: dict[str, Any]) -> float:  # noqa: ARG001 — row kept for signature symmetry
    """The DEFAULT usefulness scorer until Prompt 16 (trust) plugs in.

    NOTE (mock-first rule, PROMPTS.md hard rules): apps.trust is landing in
    this same wave and the feed MUST NOT import it. The parent wires the
    real scorer in AFTER the wave, by calling rank(..., usefulness_scorer=...)
    — the interface is exactly what a 0..1 helpful-vote ratio scorer needs.
    Never fake a signal: 0.0 is the honest "no usefulness data yet".
    """
    return 0.0


def _ward_key(value: Any) -> str:
    """Ward labels are free text from members — match on trimmed casefold
    so 'Kilimani' and ' kilimani ' agree. Empty (unknown) never matches."""
    return str(value or "").strip().casefold()


def score_row(
    row: dict[str, Any],
    *,
    context: dict[str, Any],
    usefulness_scorer: RowScorer,
) -> tuple[float, dict[str, float]]:
    """Score ONE row against the table. Returns (total, breakdown) — the
    breakdown is what templates can badge and tests can assert, so every
    ordering stays explainable at the UI level too."""
    followed_ids = context.get("followed_user_ids") or []
    followed = W_FOLLOWED if row.get("author_id") in followed_ids else 0.0

    viewer_ward = _ward_key(context.get("viewer_ward"))
    local = (
        W_LOCAL
        if viewer_ward and _ward_key(row.get("author_ward")) == viewer_ward
        else 0.0
    )

    useful_raw = usefulness_scorer(row)
    if not useful_raw:  # None/garbage scores degrade to the honest 0.0
        useful_raw = 0.0
    usefulness = USEFULNESS_SCALE * float(useful_raw)

    linked = W_LINKED if row.get("ref_guide_id") is not None else 0.0
    educational = W_EDUCATIONAL if row.get("educational") else 0.0

    viewer_groups = set(context.get("viewer_group_ids") or [])
    author_groups = set(row.get("author_group_ids") or [])
    group_overlap = W_GROUP if viewer_groups & author_groups else 0.0

    breakdown = {
        "followed": followed,
        "local": local,
        "usefulness": usefulness,
        "linked": linked,
        "educational": educational,
        "group": group_overlap,
    }
    return sum(breakdown.values()), breakdown


def rank(
    rows: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    now: Any,
    usefulness_scorer: RowScorer = zero_usefulness,
) -> list[dict[str, Any]]:
    """THE deterministic, unit-testable ranking pass. PURE: no DB, no clock
    read, no Django — rows in (new list out), originals untouched.

    - `context` — what the VIEWER brings: followed_user_ids, viewer_ward,
      viewer_group_ids (all via the other apps' PUBLIC functions).
    - `now` — the INJECTED clock. Zero table weight today; the parameter
      exists so no future time-weight can hide a timezone.now() call
      (PROMPTS.md pitfall: now() inside ranking is untestable).
    - `usefulness_scorer` — the Prompt-16 plug point (default 0.0).

    Ordering: score DESC, then created_at DESC (recency WITHIN the tier —
    the only place time appears), then row pk ASC (absolute last-resort
    tie-break so tests and pages are bit-stable). Each returned row is a
    COPY with `score` and `breakdown` added — the copy keeps callers'
    dicts reusable and makes "the list out is already sorted" safe.
    """
    # NOTE: `now` is intentionally not used in math — see module docstring.
    del now
    scored: list[dict[str, Any]] = []
    for row in rows:
        total, breakdown = score_row(
            row, context=context, usefulness_scorer=usefulness_scorer
        )
        item = dict(row)
        item["score"] = total
        item["breakdown"] = breakdown
        scored.append(item)
    scored.sort(
        key=lambda r: (
            -r["score"],
            # datetime supports no unary minus — collapse to a comparable
            # numeric (UTC seconds) so the DESC tie-break stays pure.
            -r["created_at"].timestamp(),
            r.get("pk") or 0,
        )
    )
    return scored
