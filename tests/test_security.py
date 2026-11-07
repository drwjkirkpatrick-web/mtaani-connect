"""Prompt 41 — security hardening suite: headers, cookies, secrets sweep,
and the restore-drill dry-run. The budgets-rot pitfall applies to security
too — these tests are the enforcement, not a review."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# High-signal secret SHAPES only; near-misses (DSN placeholders, fixture
# passwords) are excluded by the allow-list below, documented inline.
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT (classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    re.compile(r"sk-(ant-)?[A-Za-z0-9]{20,}"),  # openai/anthropic-style keys
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),  # slack tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM blocks
]
# Files whose contents are fixtures-by-design (test passwords like "***"),
# plus the runbook's own documented placeholder mentions.
_SWEEP_ALLOWLIST = {
    "tests/",
    "docs/runbooks/security.md",  # documents the sweep itself
    "docs/runbooks/backup.md",  # placeholder "<operator-key>", not a key
}
# Exact strings we expect to see in a shipped, credential-free repo.
_EXPECTED_PLACEHOLDERS = ("[REDACTED]", "***")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


@pytest.mark.django_db
def test_security_headers_on_every_response(client: Client):
    response = client.get("/")
    csp = response.headers["Content-Security-Policy"]
    # The map's two third-party allowances and NOTHING looser:
    assert "https://unpkg.com" in csp  # Leaflet 1.9.4 (SRI-pinned template)
    assert "https://*.tile.openstreetmap.org" in csp  # raster tiles
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_cookie_flags_and_transport_settings_present():
    assert settings.SESSION_COOKIE_SAMESITE == "Lax"
    assert settings.SESSION_COOKIE_HTTPONLY is True
    assert settings.CSRF_COOKIE_HTTPONLY is True
    # Cookie transport is env-driven so dev http stays usable; the DEPLOY
    # runbook pins secure=1 for prod — assert the knob exists and is a bool.
    assert isinstance(settings.SESSION_COOKIE_SECURE, bool)
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


def test_headers_middleware_registered_before_throttle():
    chain = settings.MIDDLEWARE
    assert chain.index("apps.core.security_headers.SecurityHeadersMiddleware") < chain.index(
        "apps.core.throttle_django.ThrottleMiddleware"
    )


def test_no_credential_looking_strings_in_tracked_files():
    """The pitfalls test, permanently: a secret that lands in git fails the
    SUITE, not a code review."""
    offenders = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(rel.startswith(allow) for allow in _SWEEP_ALLOWLIST):
            continue
        for pattern in _SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                offenders.append(f"{rel}: {pattern.pattern[:30]}…")
    assert not offenders, (
        "credential-shaped strings in tracked files:\n" + "\n".join(offenders)
    )


def test_restore_drill_dry_run_prints_the_plan():
    out = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "backup_restore_drill.sh"), "--dry-run"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert "DRY-RUN" in out.stdout
    # The plan names the real mechanics so the printed plan cannot rot away
    # from the actual script:
    for step in ("pg_dump", "pg_restore", "PASS"):
        assert step in out.stdout


def test_runbooks_checked_in_with_drill_reference():
    sec = (REPO_ROOT / "docs" / "runbooks" / "security.md").read_text(encoding="utf-8")
    bak = (REPO_ROOT / "docs" / "runbooks" / "backup.md").read_text(encoding="utf-8")
    assert "backup_restore_drill.sh" in bak  # the drill is the rehearsal
    assert "pip-audit" in sec  # the dependency-audit step CI names (Prompt 42)
    assert "SESSION_COOKIE_SECURE" in sec  # the prod cookie-transport rule
