"""Project-level views (parent-owned). Prompt 05: the language toggle.

WHY a GET toggle (not Django's POST set_language): one tap on a low-end
phone, no CSRF form ceremony — friendlier for the PWA.
"""
from django.http import Http404, HttpResponseRedirect

SUPPORTED_LANGUAGES = {"en", "sw"}


def set_lang(request, code):
    """Set the django_language cookie, then return where the user came from.

    Open-redirect guard: `next` must be a same-site path ("/...", not "//...").
    """
    if code not in SUPPORTED_LANGUAGES:
        raise Http404
    nxt = request.GET.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    response = HttpResponseRedirect(nxt)
    # WHY cookie not session: works for logged-out visitors too; 1-year persistence.
    response.set_cookie("django_language", code, max_age=365 * 24 * 3600)
    return response
