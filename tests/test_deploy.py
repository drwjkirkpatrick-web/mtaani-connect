"""Prompt 42 — the CI/deploy contract, validated WITHOUT docker (this dev
host's daemon is broken by design; pytest must never depend on it). The
YAML libraries are not pinned in pyproject, so the checks are honest
regex/structure probes of the shipped files rather than a full YAML parse —
they pin the CONTRACT (what the file must contain), not its indentation."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
COMPOSE = (REPO_ROOT / "deploy" / "docker-compose.prod.yml").read_text("utf-8")


def test_ci_names_the_budget_and_geo_suites_explicitly():
    # THE pitfall: a workflow that "runs pytest" but silently skips geo.
    assert "postgis/postgis:16-3.4" in CI  # the REAL container
    assert "SELECT extname FROM pg_extension" in CI  # extension proven present
    assert "tests/test_budgets.py" in CI  # budget suite named
    assert "tests/test_traffic.py" in CI and "tests/test_listing_search.py" in CI
    assert "makemigrations --check" in CI  # drift guard
    assert "pip-audit" in CI  # dependency audit (Prompt 41/42)
    assert "psycopg.connect" in CI  # a REAL connection, not a service echo


def test_ci_has_no_untrusted_interpolations():
    # Security-review hygiene (2026-11): no github.event payloads reach run
    # blocks; checkout stays on the implicit default ref.
    assert "github.event" not in CI
    assert "${{ github.head_ref }}" not in CI
    for trusted in ("actions/checkout@", "actions/setup-python@"):
        assert trusted in CI


def test_compose_has_the_full_stack_and_secret_gating():
    for service in ("db:", "redis:", "app:", "caddy:"):
        assert service in COMPOSE
    assert "postgis/postgis:16-3.4" in COMPOSE
    # Secrets must NOT default silently — the compose requires them:
    assert "DB_PASSWORD:?set" in COMPOSE and "SECRET_KEY:?set" in COMPOSE
    assert "${DB_PASSWORD}@db:5432/mtaani" in COMPOSE  # wire-through correct


def test_health_endpoint_answers_db_free():
    from django.test import Client

    response = Client().get("/health/")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
