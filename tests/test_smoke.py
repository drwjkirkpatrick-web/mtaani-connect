"""Prompt 01 acceptance: scaffold boots, migrates, serves a page.

WHY these three tests: each catches a different class of scaffold rot —
migrate failures (schema/engine drift), user-model drift (the AUTH_USER_MODEL
trap), and template/URL drift.
"""
import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command


def test_custom_user_is_accounts_user():
    """The custom user must be the accounts app's - never Django's default."""
    assert get_user_model()._meta.label == "accounts.User"


@pytest.mark.django_db
def test_migrate_is_idempotent():
    """pytest-django created+migrated the test DB; migrating again must no-op."""
    call_command("migrate", verbosity=0)  # raises on any pending/failed migration


@pytest.mark.django_db
def test_home_page_200(client):
    """Placeholder home renders - proves urls/templates/settings wiring."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"Mtaani" in response.content
