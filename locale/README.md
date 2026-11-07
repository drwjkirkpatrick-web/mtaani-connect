# i18n — Kiswahili completion (Prompt 35)

This directory is the CHROME language home for Mtaani Connect: Django's own
gettext machinery, LocaleMiddleware (already in MIDDLEWARE), the
`django_language` cookie (Prompt 05 toggle) and `LOCALE_PATHS` pointing here.

## What is translated

`locale/sw/LC_MESSAGES/django.po` carries Kiswahili for **UI chrome** —
nav tabs, titles, form labels/buttons, validation errors, empty states,
legal/privacy notices — every gettext marker in `apps/*` and project
templates, enumerated by `apps/core/i18n.py`. Runtime status (live numbers):
`/lang/prefs/status.json?po=1`.

## Community-reviewable workflow ("needs review" flag)

gettext has no `needs review` flag; the convention shipped here:

- `#, fuzzy` flag on an entry == **needs review**. gettext IGNORES fuzzy
  entries at runtime, so an un-reviewed sw string can never reach users —
  it renders the English fallback until a reviewer removes the flag.
- Short high-traffic strings (nav labels, common verbs/labels) ship
  WITHOUT the flag = reviewed.
- Longer strings (> 45 chars), multiline strings, and a handful of
  ambiguous single words (`on`, `at`, `for`, `filed`, `reported`,
  `showing`, `position`, `page`, `Person`, `Why`…) ship WITH the flag.
- Reviewers: open an entry, read the English msgid, adjust the sw, drop
  the `#, fuzzy` line. Entries with empty `msgstr` = untranslated,
  tracked, English fallback (NOT counted as drift — an honest partial
  state the acceptance allows).

## The drift rule (enforced by tests/test_i18n.py)

Every gettext-marker msgid in the scoped code must have a catalog record
in the sw .po. Record present but empty is fine (untranslated); RECORD
ABSENT breaks the suite. This is how "English string changed, sw catalog
did not" can never land silently. Regenerate coverage the same way it was
built: scan + fill (`apps/core/i18n.missing_sw_translations()` lists the
gap; entries are appended by the same merge logic as Prompt 35's build).

Scope note: the detector pins `apps/*` (minus `apps/ai` — Prompt 34 is an
active sibling workspace; re-include after it settles), `templates/**`,
`mtaani/**`. It does NOT pin `apps/search/locale/**` (pre-existing
app-local catalog for the same strings; the project catalog takes
precedence because LOCALE_PATHS is merged last).

## Compile

`msgfmt` is absent on the Jetson dev box. The committed `.mo` was compiled
with **polib** (`polib.pofile(po).save_as_mofile(mo)`), pure Python. At
deploy, either run the same polib one-liner or
`.venv/bin/python manage.py compilemessages` where gettext exists. The
drift test reads the `.po` SOURCES only — it never depends on the binary.

## Content language (apps.lang) vs chrome

UI chrome is en/sw only. **Content** — post bodies, guide steps, listing
descriptions, chat messages, and the Sheng-ready alias/nickname fields
(Prompt 25) — is FREE-FORM: Sheng, Kiswahili, English, or code-switched.
No content field is forced into a language and none is auto-translated.

Reading side: `ContentLanguagePreference` (`apps/lang/models.py`), default
**both**. Members choose sw / en / both; anonymous users keep theirs in
the session. POST `/lang/prefs/set/` with `value` + `next` persists and
returns to the SAME page — no full-language reload beyond that request
(chrome follows the separate `django_language` cookie).

## Mixed-feed chips (the 12-seam for the parent)

`apps/lang/chips.py::language_chip(obj)` returns
`{"code","label","css","neutral"}` — a pure display helper. Feed/post
templates wire it with either:

    {% load lang_chips %}{% lang_chip item %}

or the partial `{% include "lang/_chip.html" with chip=chip %}`.

To label an item, give its model (or the view) a `content_lang` value of
`sw` / `en` / `sheng`; anything missing or unknown renders the NEUTRAL
chip (`content` marker) — never a fabricated language claim. Suggested
parent wiring: add an optional `content_lang` CharField (max_length=16,
null=True, choices from chips) to the feed items' underlying models
(posts.Post, guides.Guide, listings.Listing, messaging threads), OR let
views attach the attr dynamically (zero migration). Chips for 'sheng'
content are intentionally first-class; posts/guides may stay unlabelled
and the mixed feed still works — that is the honest partial.
