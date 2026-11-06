"""weighting.py — PURE report weighting (Prompt 30), the Prompt-11 pattern.

NO Django imports: no settings, no ORM, no timezone.now() — the caller
injects `now` (the PROMPTS.md pitfall — a clock inside ranking is
untestable — is defeated by parameter injection, exactly like
apps/feed/ranking.py). Rows are plain dicts shaped by the caller; never
model instances, so this module stays unit-testable without any DB.

WEIGHTING FORMULA (the table is the truth — module constants below)
=================================================================
W = BASE_REPORT_WEIGHT
  × crowd_multiplier(count)      1 + log2(distinct rider count)
  × trust_factor(trust)          0.5 (unknown) .. 1.25 (max trust)
  × decay_factor(age_min)        1.0 until 45 min, then linear to 0 at 120
=================================================================
WHY each piece: BASE = one honest community report counts. crowd boost is
log2 so each DOUBLING of independent riders adds one unit — 3 riders ≈
2.59×, saturating naturally (no cap needed). Trust floors at 0.5 so a
first-time reporter is tempered, not dismissed, and ceilings at 1.25 so
even a highly trusted voice carries only ~1¼ reports — crowds and clocks
beat any single voice. Decay is HONEST SILENCE: full weight for 45 minutes
(DECAY_START_MIN, spec verbatim "decay after 45 min", then linear to zero
by DECAY_END_MIN = 120), after which weight is exactly 0.0 and the report
is no longer shown — an old condition must never masquerade as current.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable

# Declared here so retuning is a documented code change, never hidden magic.
BASE_REPORT_WEIGHT = 1.0
TRUST_MIN_FACTOR = 0.5  # unknown/beginner trust still carries half a voice
TRUST_MAX_FACTOR = 1.25  # max-trust amplification ceiling
DECAY_START_MIN = 45.0  # full weight before this age (spec: 45 min)
DECAY_END_MIN = 120.0  # linear ramp ends here; older = 0.0 (expired)

# trust_scorer(reporter) -> 0.0..1.0 — Prompt 16's scorer or a test mock.
TrustScorer = Callable[[Any], float]


def zero_trust(row: dict[str, Any]) -> float:  # noqa: ARG001 — signature symmetry
    """The DEFAULT scorer "no trust data yet" — honest 0.0, NOT a guess.

    Mock-first rule (same as feed's zero_usefulness): apps.trust is plugged
    by callers via services.make_report_scorer(); tests pass mock scorers.
    """
    return 0.0


def crowd_multiplier(count: int) -> float:
    """Independent-rider boost: 1 + log2(count) — doubling, not stacking."""
    return 1.0 + math.log2(max(1, int(count)))


def trust_factor(trust: Any) -> float:
    """Map a 0..1 usefulness score onto [TRUST_MIN_FACTOR, TRUST_MAX_FACTOR].

    Garbage (None, negative, NaN, unparseable) degrades to the unknown-trust
    floor; scores above 1.0 clamp at the ceiling — the scorer's contract
    decides what a score means, this module only maps it.
    """
    try:
        value = float(trust)
    except (TypeError, ValueError):
        value = 0.0
    if value != value:  # NaN guard without touching float("nan") elsewhere
        value = 0.0
    spread = TRUST_MAX_FACTOR - TRUST_MIN_FACTOR
    return min(TRUST_MAX_FACTOR, TRUST_MIN_FACTOR + max(0.0, value) * spread)


def decay_factor(age: Any) -> float:
    """1.0 until DECAY_START_MIN, linear to 0.0 at DECAY_END_MIN, after 0.

    Unreadable ages degrade to 0.0 — an unknown age is NOT current; silence
    beats a fabricated freshness.
    """
    try:
        age = float(age)
    except (TypeError, ValueError):
        return 0.0
    if age != age:  # NaN
        return 0.0
    if age <= DECAY_START_MIN:
        return 1.0
    if age >= DECAY_END_MIN:
        return 0.0
    span = DECAY_END_MIN - DECAY_START_MIN
    return max(0.0, 1.0 - (age - DECAY_START_MIN) / span)


def age_min(row: dict[str, Any], *, now: datetime) -> float:
    """Minutes since the row's freshest evidence — PURE (now injected)."""
    seen = row.get("last_seen_at") or row.get("created_at")
    if not hasattr(seen, "timestamp"):
        return float("inf")  # no timestamp → nothing comparable, not current
    age = (now - seen).total_seconds() / 60.0
    # A timestamp "from the future" (clock skew, fixtures) counts as
    # brand-new rather than negative-age amplified.
    return max(0.0, age)


def weight(
    row: dict[str, Any],
    *,
    trust_scorer: TrustScorer = zero_trust,
    now: datetime,
) -> float:
    """Weight ONE report row. PURE: dicts in, float out, clock injected.

    row keys consumed: merged_count, last_seen_at (falls back to
    created_at), reporter (passed to the scorer). A scorer exception
    degrades to the honest unknown-trust 0.0 — never a 500 on the feed.
    Returns 0.0 for expired reports: the caller's signal to hide them.
    """
    try:
        trust = trust_scorer(row.get("reporter"))
    except Exception:  # noqa: BLE001 — the feed must survive a broken scorer
        trust = 0.0
    return round(
        BASE_REPORT_WEIGHT
        * crowd_multiplier(int(row.get("merged_count") or 1))
        * trust_factor(trust)
        * decay_factor(age_min(row, now=now)),
        4,
    )


def weight_rows(
    rows: list[dict[str, Any]],
    *,
    trust_scorer: TrustScorer = zero_trust,
    now: datetime,
) -> list[dict]:
    """Score a list; return COPIES with ``weight`` + ``age_min`` added,
    already sorted by weight DESC, then freshest evidence, then pk —
    "freshest/merged first" at the UI level. Originals untouched.
    """
    scored = []
    for row in rows:
        item = dict(row)
        item["age_min"] = age_min(row, now=now)
        item["weight"] = weight(item, trust_scorer=trust_scorer, now=now)
        scored.append(item)
    scored.sort(
        key=lambda r: (
            -r["weight"],
            # datetime has no unary minus — collapse to UTC seconds for the
            # DESC recency tie-break (bit-stable pages, same as feed rank).
            -(r.get("last_seen_at") or r.get("created_at")).timestamp(),
            r.get("pk") or 0,
        )
    )
    return scored
