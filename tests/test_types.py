"""Prompt 02 acceptance: enums round-trip values; budget + clock utils behave.

These tests are deliberately DB-free so they run green even before the
database is provisioned (PROMPTS.md testing strategy: unit tests first).
"""
import pytest

from apps.core import types

ALL_ENUMS = [
    types.Confidence,
    types.LocationVisibility,
    types.BarterMode,
    types.OfferStatus,
    types.ReportReason,
    types.SafetyCategory,
    types.ContentStatus,
    types.ListingKind,
    types.GroupKind,
]


@pytest.mark.parametrize("enum", ALL_ENUMS)
def test_enum_values_round_trip(enum):
    """HARD RULE: what we store is .value — it must reconstruct the member."""
    for member in enum:
        assert enum(member.value) is member
        assert isinstance(member.value, str)
        assert member.value == member.value.upper()  # frozen vocabulary style


def test_confidence_honesty_states():
    """NO_DATA is a first-class state — never fabricate (product brief §9)."""
    assert types.Confidence.NO_DATA.value == "NO_DATA"
    assert types.Confidence.LIVE_GPS.value == "LIVE_GPS"
    assert types.Confidence.ESTIMATE.value == "ESTIMATE"


def test_offer_status_values_match_state_machine():
    """The barter state machine vocabulary (validated further in Prompt 21)."""
    for value in ["DRAFT", "OFFERED", "COUNTERED", "ACCEPTED", "COMPLETED",
                  "CANCELLED", "EXPIRED"]:
        assert types.OfferStatus[value].value == value


def test_safety_categories_present():
    for value in ["MEDICAL", "ELECTRICAL", "CONSTRUCTION", "NONE"]:
        assert types.SafetyCategory[value].value == value


def test_budget_util_fails_oversized():
    from apps.core.testing import assert_under_budget

    with pytest.raises(AssertionError):
        assert_under_budget("x" * (60 * 1024), kb=50)


def test_budget_util_passes_small_page():
    from apps.core.testing import assert_under_budget

    assert_under_budget("<html>" + "x" * 1000 + "</html>", kb=50)


def test_mock_now_is_fixed_utc():
    from apps.core.testing import mock_now

    now = mock_now()
    assert (now.year, now.month, now.day) == (2026, 1, 15)
    assert now.tzinfo is not None  # UTC-aware, never naive
