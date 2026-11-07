"""TEST-ONLY URLconf + ingest stub for Prompt 29 (apps.crew — owned file).

WHY a test URLconf at all: root mtaani/urls.py is PARENT-OWNED and wires
the crew app at consolidation; my tests run BEFORE that. They mount THIS
module via ``override_settings(ROOT_URLCONF="apps.crew.tests_urls")`` so
every ``reverse("crew:…")`` resolves without touching shared files. It
also includes the sibling URLconfs the base template's tabbar links need.

THE INGEST STUB stands in for 28's real endpoint at the DOCUMENTED
provisional contract (also pinned in my tests):

    POST  /tracking/ingest/<session_id>/
    header  X-Session-Token: <raw token>          (seam header name)
    body    {"vehicle_id": <int>, "pings": [ {"recorded_at": ISO,
              "lat": <float>, "lon": <float>, "speed_kmh": <int>}, … ]}
            batch ≤ 50                            (seam payload schema)
    reply   {"accepted": true, "duplicates": <int>, "downgraded": <int>}

It records each POST (path, header token, parsed body) into ``STUB.calls``
and answers per ``STUB.mode`` — "accepted" (200, accepted true) or
"refused" (503, accepted false). NOTHING more: the stub is a mock of the
seam, not of 28's pipeline. CSRF is disabled (it mocks a token-
authenticated batch API, like the map's, not a session-form endpoint).
Tests assert: posted EXACTLY ONCE per flush with the token header, the
buffer cleared ONLY on accepted:true, kept whole offline/refused.
"""
from __future__ import annotations

import json

from django.http import JsonResponse
from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from mtaani import views as root_views  # root's set_lang (base.html lang bar)


class _Stub:
    """One mutable holder the tests drive: mode + the call log."""

    calls: list = []
    mode: str = "accepted"  # "accepted" | "refused"


STUB = _Stub()


def _answer(accepted: bool, status: int) -> JsonResponse:
    return JsonResponse(
        {"accepted": accepted, "duplicates": 0, "downgraded": 0}, status=status
    )


@csrf_exempt  # TEST-ONLY stub: token-authenticated JSON API (28's contract)
@require_POST
def ingest_stub(request, session_id: int):
    token = request.headers.get("X-Session-Token", "")
    try:
        body = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        STUB.calls.append(
            {"session_id": session_id, "token": token, "path": request.path, "body": None}
        )
        return _answer(False, 400)
    STUB.calls.append(
        {"session_id": session_id, "token": token, "path": request.path, "body": body}
    )
    if STUB.mode != "accepted":
        return _answer(False, 503)
    return _answer(True, 200)


urlpatterns = [
    # TEST-ONLY ingest stub answering 29's DOCUMENTED provisional ingest
    # URL while 28's real endpoint is not wired; consolidation binds the
    # SEAM constants in apps.crew.views to the real route instead.
    path("tracking/ingest/<int:session_id>/", ingest_stub, name="crew_test_ingest"),
    # Production wiring (mirrors mtaani/urls.py; the crew line is what the
    # parent adds there):
    path("accounts/", include("apps.accounts.urls")),
    path("guides/", include("apps.guides.urls")),
    path("market/", include("apps.listings.urls")),
    path("chats/", include("apps.messaging.urls")),
    path("routes/", include("apps.routes.urls")),
    path("fleet/", include("apps.fleet.urls")),
    path("tracking/", include("apps.tracking.urls")),
    path("crew/", include("apps.crew.urls")),
    path("transport/", TemplateView.as_view(template_name="stub.html"), name="transport_stub"),
    path("lang/<str:code>/", root_views.set_lang, name="set_lang"),
]
