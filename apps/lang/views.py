"""Content-language preference views (PROMPTS.md Prompt 35).

One-tap POST toggle that lands back on the SAME page (?next=) — the spec's
"no page re-load required beyond the request". Signed-in: durable row on
the user. Anonymous: session fallback. Invalid values are refused politely
(HTTP 400 with a short message), never coerced.

Routes (names are global because the parent includes WITHOUT a namespace):
- POST /lang/prefs/set/        name="lang_prefs_set"
- GET  /lang/prefs/status.json name="lang_prefs_status"
- GET  /lang/prefs/demo/       name="lang_prefs_demo" (chips demo page)
These are 3-segment paths so they never collide with the Prompt 05
chrome toggle /lang/<code>/ (2 segments, owned by mtaani/views.py).
"""
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils.translation import gettext as gettext_now
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.core.i18n import (
    DEFAULT_CONTENT_LANG,
    SPEC_CONTENT_LANGS,
    normalize_content_lang,
    translation_status,
)
from apps.lang.chips import chips_for
from apps.lang.models import ContentLanguagePreference

SESSION_KEY = "content_lang"

_PREFERENCE = {"en", "sw", "both"}
_BAD_PREFERENCE_TEXT = "That content language choice is not one we support."


def resolve_content_lang(user, session) -> str:
    """Effective preference: user row > session fallback > default 'both'."""
    if user is not None and getattr(user, "is_authenticated", False):
        row = ContentLanguagePreference.objects.filter(user=user).first()
        if row:
            return row.value
    if session is not None:
        normalized = normalize_content_lang(session.get(SESSION_KEY))
        if normalized:
            return normalized
    return DEFAULT_CONTENT_LANG


def _safe_next(request, posted=False) -> str:
    """Same-site only ('/...' not '//...'), default '/'."""
    source = request.POST if posted else request.GET
    nxt = source.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        return "/"
    return nxt


@require_POST
@csrf_protect
def set_preference(request):
    """Persist en/sw/both and return to ?next= (same page, one request)."""
    value = normalize_content_lang(request.POST.get("value"))
    nxt = _safe_next(request, posted=True)
    if value is None:
        return HttpResponseBadRequest(_BAD_PREFERENCE_TEXT)
    if request.user.is_authenticated:
        ContentLanguagePreference.objects.update_or_create(
            user=request.user, defaults={"value": value}
        )
    else:
        request.session[SESSION_KEY] = value
        request.session.modified = True
    response = HttpResponseRedirect(nxt)
    # One-tap UX back on the SAME page: a short-lived flash cookie the page
    # chrome can surface; the preference itself is the durable state.
    response.set_cookie("content_lang_flash", value, max_age=60, samesite="Lax")
    return response


def status(request):
    """JSON: active preference (+ optional honest sw-catalog status)."""
    data = {
        "content_lang": resolve_content_lang(request.user, request.session),
        "supported_content_langs": list(SPEC_CONTENT_LANGS),
    }
    if request.GET.get("po") == "1":
        try:
            data["po"] = translation_status()
        except Exception:
            data["po"] = {"error": "catalog unavailable"}
    return JsonResponse(data)


def demo(request):
    """Honest chips demo: sample items with varied language metadata."""
    class _Sample:
        def __init__(self, title, content_lang=None):
            self.title = title
            self.content_lang = content_lang

    items = [
        _Sample("Ugali ni nini?", "sw"),
        _Sample("Fix a leaking tap", "en"),
        _Sample("Kalee tuone kama ipesha", "sheng"),
        _Sample("Mchungaji wa kibanda", None),
    ]
    po_status = None
    try:
        po_status = translation_status()
    except Exception:
        po_status = None
    return render(
        request,
        "lang/prefs_demo.html",
        {
            "current": resolve_content_lang(request.user, request.session),
            "items": items,
            "chips": chips_for(items),
            "zip": list(zip(items, chips_for(items))),
            "po_status": po_status,
            "next": _safe_next(request),
        },
    )
