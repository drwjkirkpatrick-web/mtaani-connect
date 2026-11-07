"""Merchant tier feature FLAGS (PROMPTS.md Prompt 38) — PURE lookups only.

This module is the policy registry; it grants NOTHING (the acceptance: a
tier flip requires a staff action + an audit row — services.py). It answers
"what does tier X allow" so surfaces render honest caps:

- portfolio_size_cap — the max ACTIVE listings/portfolios a merchant may
  run: FREE = 8, mirroring Prompt 18's portfolio cap (apps/services
  MAX_IMAGES = 8), PRO = 25 (bought headroom, not yet sold).
- refresh_cadence_hours — the minimum hours between refreshing (the
  bump-like window) of ONE merchant row: FREE = 7 days (168 h), PRO = 1
  day (24 h). ENFORCEMENT lives in the owning apps' gates (a documented
  parent seam — this app only looks the policy up).

Pure: no DB, no clock, no Django. Unknown / None / malformed tier values
degrade to FREE — the tier of an absent enrollment IS the free tier, never
a crash, never an accidental grant.
"""
from __future__ import annotations

from dataclasses import dataclass


FREE_TIER_PORTFOLIO_CAP = 8    # mirrors Prompt 18's portfolio cap (services MAX_IMAGES)
PRO_TIER_PORTFOLIO_CAP = 25
FREE_TIER_REFRESH_CADENCE_HOURS = 168   # a week — merchants wait like everyone else
PRO_TIER_REFRESH_CADENCE_HOURS = 24     # a day — the bought headroom

_TIER_FREE = "FREE"
_TIER_PRO = "PRO"


@dataclass(frozen=True)
class MerchantFeatureSet:
    """The flags only — what a tier CAN do, with its bilingual tier copy."""

    tier: str
    label_en: str
    label_sw: str
    portfolio_size_cap: int
    refresh_cadence_hours: int


FEATURE_SETS: dict[str, MerchantFeatureSet] = {
    _TIER_FREE: MerchantFeatureSet(
        tier=_TIER_FREE,
        label_en="Free",
        label_sw="Bure",
        portfolio_size_cap=FREE_TIER_PORTFOLIO_CAP,
        refresh_cadence_hours=FREE_TIER_REFRESH_CADENCE_HOURS,
    ),
    _TIER_PRO: MerchantFeatureSet(
        tier=_TIER_PRO,
        label_en="Merchant Pro",
        label_sw="Wauzaji Pro",
        portfolio_size_cap=PRO_TIER_PORTFOLIO_CAP,
        refresh_cadence_hours=PRO_TIER_REFRESH_CADENCE_HOURS,
    ),
}

_FREE = FEATURE_SETS[_TIER_FREE]


def features(tier) -> MerchantFeatureSet:
    """Pure lookup; anything unknown degrades to FREE (never a grant)."""
    value = getattr(tier, "value", tier)  # accept the enum member or its .value
    if isinstance(value, str):
        return FEATURE_SETS.get(value.strip().upper(), _FREE)
    return _FREE


def portfolio_size_cap(tier) -> int:
    return features(tier).portfolio_size_cap


def refresh_cadence_hours(tier) -> int:
    return features(tier).refresh_cadence_hours
