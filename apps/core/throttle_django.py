"""throttle_django.py — the web-coupled thin layer for Prompt 36 throttles.

 PURE RULE (apps/core/throttle.py): the engine is framework-free; THIS file
 is the only Django-coupled piece in Prompt 36's slice. It owns:

 - class resolution: (app namespace, route name) maps to an endpoint class,
   default map below, overridable via ``settings.THROTTLE_RESOLVER_CLASSES``
   (a full REPLACEMENT of the same-shape dict);
 - caller identity: authenticated user pk (falls back to the IP bucket when
   anonymous — unknown identity degrades to IP), client IP from REMOTE_ADDR;
 - HTTP surface: 429 + ``Retry-After`` + ``X-RateLimit-Remaining`` (+limit)
   headers on refusals, ``X-RateLimit-Remaining`` on allowed responses;
 - the warn flag: ``request.throttle_warning = {...}`` set whenever an
   over-limit rejection (soft warning) was issued on this request.

 PARENT JOB (documented exact snippet, see the final report's
 ``middleware_snippet``): register ``apps.core.throttle_django.ThrottleMiddleware``
 in ``MIDDLEWARE`` — AFTER ``AuthenticationMiddleware`` (it reads
 ``request.user``) and last (it must see the final identity, and a refused
 request must never touch a view or session writes).

 Per-view wiring of EXISTING doors (posting/messaging/uploads) can use
 ``check_endpoint(request, "class")`` — returns ``HttpResponse | None``
 (None = pass). Keep the DB-backed doors of 04/15 (they are the authority
 for auth codes / reports); this middleware ADDS the systematic buckets.

 WHY time.time() numbers: the pure engine's clock is a number; Django code
 converts at this seam only (``.timestamp()`` / monkeypatchable ``_now``).
"""
from __future__ import annotations

import time

from django.conf import settings
from django.http import HttpResponse
from django.urls import Resolver404, resolve
from django.utils.translation import gettext_lazy as _

from apps.core import throttle
from apps.core.throttle import Verdict, enforce, get_store, load_rules

#: (app_name, url_name) -> endpoint class. Replacement-overridable via
#: ``settings.THROTTLE_RESOLVER_CLASSES`` (same shape fully replaces this).
#: Routes ABSENT from the map are unthrottled — the fail-open rule for
#: unconfigured classes; adding a route here is the ONLY way a class applies.
DEFAULT_RESOLVER_CLASSES = {
    # Prompt 04 auth surface (door stays; middleware mirrors it server-side)
    ("accounts", "signup"): "auth",
    ("accounts", "login"): "auth",
    ("accounts", "verify_email"): "auth",
    ("accounts", "verify_phone"): "auth",
    ("accounts", "recover_request"): "auth",
    ("accounts", "recover_reset"): "auth",
    # Prompt 10 posting surface (create/edit/delete are THE spam walls)
    ("posts", "create"): "posting",
    ("posts", "edit"): "posting",
    ("posts", "delete"): "posting",
    ("guides", "create"): "posting",
    ("guides", "edit"): "posting",
    ("guides", "delete"): "posting",
    ("guides", "publish"): "posting",
    ("listings", "create"): "posting",
    ("listings", "edit"): "posting",
    ("listings", "remove"): "posting",
    ("services", "create"): "posting",
    ("services", "edit"): "posting",
    ("services", "remove"): "posting",
    # Prompt 20/23 reports (numbers mirrored from Prompt 15's DB door)
    ("moderation", "report_create"): "reports",
    ("messaging", "report_message"): "reports",
    ("reviews", "report"): "reports",
    # Prompt 26 messaging sends (detail POST writes a message;
    # start_for_listing opens threads — counted as sends)
    ("messaging", "detail"): "messaging_send",
    ("messaging", "start_for_listing"): "messaging_send",
    # Prompt 06 uploads (media is size-capped; the DAY bucket is new here)
    ("posts", "image_upload"): "uploads",
    ("listings", "image_upload"): "uploads",
    ("services", "image_upload"): "uploads",
    ("profiles", "me_avatar"): "uploads",
}

REJECT_BODY_429 = _(
    "Too many requests. Please wait a moment and try again."  # gettext-mark
)


def _now() -> float:
    """Seam for the injected clock (middleware reads ``time.time`` numbers;
    tests monkeypatch ``ThrottleMiddleware._clock`` or pass ``now``)."""
    return time.time()


def _client_ip(request) -> str | None:
    """Client identity BY ADDRESS — read server-side only (PROMPTS.md pitfall:
    anything client-side is a suggestion; and this layer refuses to trust a
    spoofable header — the real X-Forwarded-From-Proxy wiring is 42's)."""
    return request.META.get("REMOTE_ADDR") or None


def _user_key(request) -> str | None:
    """Authenticated pk string; anonymous -> None (the engine's anon/IP
    downgrade applies)."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if user.pk is None:
        return None
    return str(user.pk)


def resolve_endpoint_class(request) -> str | None:
    """(app_name, url_name) via the resolver match -> endpoint class.

    Middleware runs pre-view, so ``resolver_match`` is usually unset — an
    explicit ``resolve(path)`` fills it (cached in the request so later
    stages don't re-resolve). Unresolvable paths (static, 404s) -> None:
    pages 404 anyway, and fail-open for the unknown keeps this side clean.
    """
    match = getattr(request, "resolver_match", None)
    if match is None:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None
        request.resolver_match = match
    app_name = getattr(match, "app_name", None)
    url_name = getattr(match, "url_name", None)
    if app_name is None or url_name is None:
        return None
    mapping = getattr(settings, "THROTTLE_RESOLVER_CLASSES", None)
    if mapping is None:
        mapping = DEFAULT_RESOLVER_CLASSES
    return mapping.get((app_name, url_name))


def check_endpoint(request, endpoint_class: str, *, store=None, now=None,
                   rules=None) -> Verdict:
    """Pure-python-friendly one-stop: classify identity and consume
    ``endpoint_class``. Returns the Verdict; NEVER raises on a configured
    class (bad limits fail-fast upstream; unknown classes fail open)."""
    rules = rules if rules is not None else load_rules(
        override=getattr(settings, "THROTTLE_RULES_OVERRIDE", None)
    )
    return enforce(
        store if store is not None else get_store(),
        endpoint_class=endpoint_class,
        user_key=_user_key(request),
        ip_key=_client_ip(request),
        now=_now() if now is None else now,
        rules=rules,
    )


def refusal_response(verdict: Verdict) -> HttpResponse:
    """429 + RFC/Ratelimit headers + gettext'd plain body (mobile-first: no
    template round-trip for the worst-case page)."""
    body = str(REJECT_BODY_429)
    response = HttpResponse(
        body, status=429, content_type="text/plain; charset=utf-8"
    )
    for name, value in verdict.to_headers().items():
        response[name] = value
    return response


class ThrottleMiddleware:
    """Prompt 36 middleware — the server-side gate (parent registers it).

    Refused requests NEVER reach the view (throttles live server-side;
    nothing here trusts a client-supplied limit, counter or clock).
    Allowed responses get ``X-RateLimit-Remaining`` (observability for
    honest clients); refused ones get 429 + ``Retry-After`` + both
    X-RateLimit headers.
    """

    def __init__(self, get_response) -> None:
        self.get_response = get_response

    def _store(self):
        """Process store; the Django wiring seam. Default = the module's
        MemoryThrottleStore (single-process dev/tests). Prompt 42 swaps it
        for Redis via ``throttle.set_store`` at boot, NEVER per-request."""
        return get_store()

    def __call__(self, request):
        endpoint_class = resolve_endpoint_class(request)
        if endpoint_class is None:
            return self.get_response(request)
        verdict = check_endpoint(
            request, endpoint_class, store=self._store()
        )
        if not verdict.allowed:
            request.throttle_warning = {  # the warn flag (soft-warning stage)
                "endpoint_class": endpoint_class,
                "kind": verdict.kind,
                "penalty_minutes": verdict.penalty_minutes,
                "retry_after": verdict.retry_after,
            }
            return refusal_response(verdict)
        response = self.get_response(request)
        remaining = verdict.remaining
        if remaining is not None:
            response["X-RateLimit-Remaining"] = str(max(0, int(remaining)))
            if verdict.limit is not None:
                response["X-RateLimit-Limit"] = str(int(verdict.limit))
        return response
