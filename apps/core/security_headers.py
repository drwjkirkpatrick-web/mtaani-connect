"""Prompt 41 — security headers, applied as responses leave every view.

Django's XFrameOptionsMiddleware covers frame-deny; this middleware adds the
rest (CSP from settings.CONTENT_SECURITY_POLICY, nosniff, referrer, COOP).
Headers belong in a middleware (not @decoration) so EVERY surface — including
the framework's own (403 CSRF pages) — leaves hardened.
"""
from __future__ import annotations

from django.conf import settings


def _csp_value() -> str:
    return "; ".join(
        f"{directive} {' '.join(sources)}"
        for directive, sources in settings.CONTENT_SECURITY_POLICY.items()
    )


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", _csp_value())
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Referrer-Policy", settings.SECURE_REFERRER_POLICY
        )
        response.headers.setdefault(
            "Cross-Origin-Opener-Policy",
            settings.SECURE_CROSS_ORIGIN_OPENER_POLICY,
        )
        return response
