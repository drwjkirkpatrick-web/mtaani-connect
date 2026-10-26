"""Shared pytest utilities (PROMPTS.md Prompt 02).

NOTE: import from tests as `from apps.core.testing import ...` — pytest runs
from the repo root so `apps` is importable without installation.

HARD RULE these enforce: mobile-first data budgets (~KES 10 buys 70 MB
daily on Kenyan prepaids — every KB is the user's money) and injected
clocks (time-dependent logic must be testable and deterministic).
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import pytest


def assert_under_budget(html: str, kb: float) -> None:
    """Assert rendered HTML stays under its data budget.

    Usage: assert_under_budget(response.content.decode(), kb=50)
    WHY a hard util: budgets rot as features land — this makes them tests.
    """
    size_kb = len(html.encode("utf-8")) / 1024
    assert size_kb <= kb, (
        f"page weight {size_kb:.1f} KB exceeds the {kb} KB budget — trim before landing"
    )


def mock_now() -> datetime:
    """A fixed UTC moment for deterministic tests.

    WHY: modules that depend on time must accept an injectable clock
    (feed ranking, decay, expiry, throttles all do).
    """
    return datetime(2026, 1, 15, 9, 30, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def fresh_user(db):
    """An ordinary verified member - nothing more (no roles, no content)."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    return User.objects.create_user(
        username="wanjiku", email="wanjiku@example.com", password="pass-123456"
    )


@pytest.fixture
def client_auth(client, fresh_user):
    """A test client logged in as fresh_user."""
    client.force_login(fresh_user)
    return client
