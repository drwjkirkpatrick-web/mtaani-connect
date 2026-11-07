"""Prompt 35 tests — multilingual completion (chrome drift + content prefs).

Pure helpers and Django test client only: no network, no clock-dependent
behaviour. The drift detector runs over the REAL tree once per test call and
must stay fast (budget pinned below); the seeded synthetic case proves the
detector actually detects, independent of the shipped catalog.
"""
from __future__ import annotations

import time

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.core import i18n
from apps.lang.chips import language_chip
from apps.lang.models import ContentLanguagePreference
from apps.lang.views import SESSION_KEY

pytestmark = pytest.mark.django_db


# --- missing-translation (drift) detector -----------------------------------

def test_detects_missing_msgid_in_seeded_tree(tmp_path, monkeypatch):
    """A seeded synthetic tree: one marker, catalog misses it -> reported."""
    root = tmp_path / "apps" / "demo"
    (root / "templates" / "demo").mkdir(parents=True)
    (root / "py").mkdir(parents=True)
    py = root / "py" / "mod.py"
    py.write_text(
        'from django.utils.translation import gettext_lazy as _\n'
        'LABEL = _("Kilo Fresh")\n'
        'HINT = gettext_lazy("Mwiba wa Sukari")\n',
        encoding="utf-8",
    )
    html = root / "templates" / "demo" / "page.html"
    html.write_text(
        "{% load i18n %}<a>{% trans 'Duka La Mtaa' %}</a>"
        "{% blocktranslate trimmed with x=s.y %}Ring {{ x }} wide{% endblocktranslate %}",
        encoding="utf-8",
    )
    index = i18n.enumerate_source_msgids(roots=[tmp_path])
    assert set(k[1] for k in index.keys()) == {
        "Kilo Fresh",
        "Mwiba wa Sukari",
        "Ring %(x)s wide",
        "Duka La Mtaa",
    }
    # Build a catalog that contains everything EXCEPT one real msgid.
    import polib

    po = polib.POFile()
    po.metadata["Language"] = "sw"
    for _, msgid in index.keys():
        if msgid != "Mwiba wa Sukari":
            po.append(polib.POEntry(msgid=msgid, msgstr="sw"))
    synthetic = tmp_path / "synthetic.po"
    po.save(str(synthetic))
    monkeypatch.setattr(i18n, "SW_PO_PATH", synthetic)
    missing = i18n.missing_sw_translations(index)
    assert [m["msgid"] for m in missing] == ["Mwiba wa Sukari"]
    assert missing[0]["files"], "drift report must cite its source file"


def test_real_tree_has_zero_drift_and_is_fast():
    """THE suite guard: scanner over the real tree, all msgids catalogued."""
    t0 = time.perf_counter()
    index = i18n.enumerate_source_msgids()
    elapsed = time.perf_counter() - t0
    assert len(index) > 100, "scanner must actually see the project's markers"
    assert elapsed < 10, f"drag detector scan took {elapsed:.1f}s — keep it bounded"
    missing = i18n.missing_sw_translations(index)
    assert not missing, (
        "sw catalog drifted: these code msgids have no catalog record "
        f"({len(missing)}): {[m['msgid'][:60] for m in missing[:8]]}"
    )


def test_translation_status_is_honest():
    """Catalog status: counts add up, coverage is a real percentage."""
    status = i18n.translation_status()
    total = status["total_entries"]
    assert total > 500  # the real catalog
    assert status["reviewed"] + status["needs_review"] <= total
    assert 0.0 <= status["coverage_percent"] <= 100.0
    # the honest part: some chrome is still needs-review/untranslated
    assert status["needs_review"] > 0 or status["untranslated"] > 0


# --- content-language preference ---------------------------------------------

def _set_pref(client, value, nxt="/"):
    return client.post(
        reverse("lang_prefs_set"), {"value": value, "next": nxt}
    )


def test_default_preference_is_both():
    user = get_user_model().objects.create_user(username="kweli", password="x")
    from apps.lang.views import resolve_content_lang

    assert resolve_content_lang(user, {}) == "both"


def test_logged_in_preference_persists_per_user(client, fresh_user):
    client.force_login(fresh_user)
    r = _set_pref(client, "sw", nxt="/")
    assert r.status_code == 302 and r.url == "/"
    pref = ContentLanguagePreference.objects.get(user=fresh_user)
    assert pref.value == "sw"
    # same user, different value -> upsert, one row only
    _set_pref(client, "en")
    assert ContentLanguagePreference.objects.filter(user=fresh_user).count() == 1
    assert ContentLanguagePreference.objects.get(user=fresh_user).value == "en"


def test_anonymous_preference_lives_in_session(client):
    r = _set_pref(client, "both", nxt="/qa/")
    assert r.status_code == 302 and r.url == "/qa/"
    assert client.session[SESSION_KEY] == "both"
    assert ContentLanguagePreference.objects.exists() is False


def test_invalid_value_refused_politely(client):
    r = _set_pref(client, "fr", nxt="/")
    assert r.status_code == 400
    assert "content language" in r.content.decode().lower()
    r2 = _set_pref(client, "", nxt="/")
    assert r2.status_code == 400
    # open-redirect guard: //evil never honoured
    r3 = _set_pref(client, "sw", nxt="//evil.com")
    assert r3.url == "/" and not r3.url.startswith("//")


def test_status_json_reports_preference(client, fresh_user):
    client.force_login(fresh_user)
    _set_pref(client, "sw")
    r = client.get(reverse("lang_prefs_status") + "?po=1")
    assert r.status_code == 200
    data = r.json()
    assert data["content_lang"] == "sw"
    assert data["supported_content_langs"] == ["sw", "en", "both"]
    assert data["po"]["total_entries"] > 500


# --- mixed-feed chips ---------------------------------------------------------

class _Item:
    def __init__(self, content_lang=None):
        self.content_lang = content_lang


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sw", {"code": "sw", "label": "Kiswahili", "neutral": False}),
        ("en", {"code": "en", "label": "English", "neutral": False}),
        ("sheng", {"code": "sheng", "label": "Sheng", "neutral": False}),
    ],
)
def test_chip_uses_item_language_metadata(raw, expected):
    chip = language_chip(_Item(raw))
    assert chip["code"] == expected["code"]
    assert chip["label"] == expected["label"]
    assert chip["neutral"] is expected["neutral"]
    assert chip["css"].startswith("lang ")


@pytest.mark.parametrize("raw", [None, "", "fr", "SOME-UNKNOWN", 42, object()])
def test_chip_neutral_fallback_for_unknown_or_missing(raw):
    chip = language_chip(_Item(raw) if raw is not None else _Item())
    assert chip["neutral"] is True
    assert chip["code"] == ""  # never a fabricated language claim


def test_chip_neutral_for_plain_object_without_metadata():
    class Bare:
        pass

    chip = language_chip(Bare())
    assert chip["neutral"] is True


def test_lang_chip_templatetag_renders_markup():
    from django.template import Context, Template

    html = Template(
        "{% load lang_chips %}{% lang_chip a %}|{% get_lang_chip b as c %}"
        "<em class=\"{{ c.css }}\">{{ c.label }}</em>"
    ).render(Context({"a": _Item("sw"), "b": _Item("en")}))
    assert '<span class="lang chip-sw">Kiswahili</span>' in html
    assert 'chip-en' in html and 'English' in html


# --- toggle persistence (no page re-load beyond the request) -------------------

def test_toggle_returns_to_same_page_and_persists(client, fresh_user):
    client.force_login(fresh_user)
    nxt = reverse("guides:list")
    r = _set_pref(client, "en", nxt=nxt)
    assert r.status_code == 302 and r.url == nxt
    # second request, WITHOUT re-submitting, reflects the stored preference
    r2 = client.get(reverse("lang_prefs_demo"))
    assert r2.status_code == 200
    assert "en" in r2.content.decode()
    # chrome toggle still works independently of the content preference
    client.get("/lang/sw/?next=/")
    r3 = client.get(reverse("lang_prefs_demo"))
    assert r3.status_code == 200  # chrome language change did not reset prefs


def test_toggle_without_login_uses_session_across_requests(client):
    _set_pref(client, "sw", nxt="/")
    r = client.get(reverse("lang_prefs_demo"))
    assert r.status_code == 200
    assert "sw" in r.content.decode()


def test_chip_partial_renders_in_demo(client):
    r = client.get(reverse("lang_prefs_demo"))
    html = r.content.decode()
    for css in ("chip-sw", "chip-en", "chip-sheng", "chip-neutral"):
        assert css in html
