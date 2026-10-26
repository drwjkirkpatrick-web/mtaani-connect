"""Prompt 05 acceptance: PWA shell honest, bilingual, and inside data budgets.

Budgets are HARD RULES (~KES 10 buys 70 MB daily): the tests make rot visible.
"""
import json
import os
from pathlib import Path

import pytest

from apps.core.testing import assert_under_budget

STATIC = Path(__file__).resolve().parent.parent / "static"
NAV_LABELS = ["Home", "Guides", "Market", "Chats", "Transport"]


@pytest.mark.django_db
def test_home_renders_tabbar_under_budget(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert 'class="tabbar"' in html
    for label in NAV_LABELS:
        assert label in html  # English default locale
    assert "lang/en/" in html and "lang/sw/" in html
    assert_under_budget(html, kb=50)


def test_css_sheet_under_15kb_budget():
    size_kb = os.path.getsize(STATIC / "css/mtaani.css") / 1024
    assert size_kb <= 15, f"CSS grew to {size_kb:.1f} KB — trim before landing"


def test_manifest_valid_and_linked():
    """File-level assertions: URL serving is deployment wiring (runserver in
    dev, reverse proxy in prod) — verified by the runserver smoke check, not
    by the test client, which does not serve /static/ under DEBUG=False."""
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["name"] == "Mtaani Connect"
    assert data["start_url"] == "/" and data["display"] == "standalone"
    assert any(int(i["sizes"].split("x")[0]) >= 192 for i in data["icons"])
    # the shell must actually link it
    assert "manifest.webmanifest" in (STATIC.parent / "templates/base.html").read_text(encoding="utf-8")


def test_sw_source_gates():
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    # PITFALL guard: cache name must be derived from a version constant
    assert 'CACHE_VERSION = "mtaani-v' in src
    assert 'SHELL_CACHE = "mtaani-shell-" + CACHE_VERSION' in src
    # HARD RULES: GET-only, auth pages never cached
    assert 'request.method !== "GET"' in src
    assert '"/accounts/"' in src
    # and base.html's app.js is what registers the SW
    assert "sw.js" in (STATIC / "js/app.js").read_text(encoding="utf-8")


def test_offline_page_self_contained(client):
    response = client.get("/offline/")
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert "offline" in html.lower()
    assert '<link rel="stylesheet"' not in html  # zero external requests
    assert_under_budget(html, kb=50)


@pytest.mark.parametrize("route", ["/guides/", "/market/", "/chats/", "/transport/"])
def test_stub_routes_render(client, route):
    response = client.get(route)
    assert response.status_code == 200
    assert "Coming soon" in response.content.decode("utf-8")


@pytest.mark.django_db
def test_language_toggle_to_sw_and_back(client):
    r = client.get("/lang/sw/?next=/")
    assert r.status_code == 302 and r.url == "/"
    assert "django_language" in r.cookies
    html = client.get("/").content.decode("utf-8")
    assert "Nyumbani" in html and "Soko" in html  # Kiswahili nav live
    # and back to English
    client.get("/lang/en/?next=/")
    html = client.get("/").content.decode("utf-8")
    assert "Home" in html


@pytest.mark.django_db
def test_language_toggle_rejects_unknown_code(client):
    assert client.get("/lang/fr/").status_code == 404


@pytest.mark.django_db
def test_language_toggle_blocks_open_redirect(client):
    r = client.get("/lang/sw/?next=https://evil.example.com")
    assert r.status_code == 302 and r.url == "/"  # same-site paths only


def test_kiswahili_catalog_covers_nav():
    """Every nav/chrome msgid must have a non-empty Kiswahili msgstr."""
    import polib

    po = polib.pofile(str(STATIC.parent / "locale/sw/LC_MESSAGES/django.po"))
    for label in NAV_LABELS + ["You are offline", "Coming soon", "Log in", "Join"]:
        entry = po.find(label)
        assert entry is not None, f"missing msgid: {label!r}"
        assert entry.msgstr.strip(), f"empty Kiswahili translation for {label!r}"


def test_base_template_wraps_nav_in_trans():
    src = (STATIC.parent / "templates/base.html").read_text(encoding="utf-8")
    for label in NAV_LABELS:
        assert '{% trans "' + label + '" %}' in src


def test_icon_files_exist_and_are_png():
    for size in (192, 512):
        p = STATIC / "icons" / f"icon-{size}.png"
        assert p.exists() and p.stat().st_size > 0
        assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
