"""labeling.py — the AI-content labeling contract seam (Prompt 33 ⇄ 12).

Prompt 12's provenance contract, and how this app plugs into it:

- DATA-DRIVEN FLAG: machine answers carry ``Answer.is_machine`` — a plain
  boolean column on the row (qa's models), set ONLY through
  ``apps.qa.services.set_machine_answer`` (no form, serializer or view
  touches the field, so no surface can produce an unlabeled AI answer by
  forgetting markup anywhere). Templates read ``{% if a.is_machine %}``.
- THE BADGE PAIR: qa renders that flag as
  ``<span class="badge badge-ai">{% trans "AI answer — verify before
  relying on it" %}</span>`` — the text lives in qa's template; NOTHING
  shared/importable existed, and this app must not edit qa (one agent
  per file), so the badge pair for AI-ASSISTED content (drafted with AI
  help) is defined HERE and mirrors qa's class exactly.

THE 12-SEAM DECISION (for the parent to unify): ``AI_BADGE_CLASS`` keeps
qa's exact ``badge-ai`` value so the one badge stylesheet colors both
markers; ``AI_LABEL_TEXT`` is "AI-assisted …" — the sibling of qa's
"AI answer …" wording, for AI-ASSISTED rather than fully machine
content (KDPA context: "AI-assisted" marker for content built with AI
help). If the parent later hoists the badge markup into shared template
code (e.g. templates/_ai_badge.html), swap both call sites to it; until
then each app renders its own marker with the shared class, and the
data-driven rule stays authoritative for answers.
"""
from django.utils.translation import gettext_lazy as _

#: Shared CSS marker (Prompt 12 renders the same class for AI answers).
AI_BADGE_CLASS = "badge-ai"

#: Sibling of qa's "AI answer — verify before relying on it" badge: same
#: promise, for AI-ASSISTED content. gettext-marked (i18n hard rule).
AI_LABEL_TEXT = _("AI-assisted — verify before relying on it")


def ai_label() -> tuple[str, str]:
    """``(label_text, css_class)`` — the pair a consumer's template prints.

    WHY a function returning a tuple: text resolves AT RENDER TIME (the
    class attr above stays lazy), so translations follow the request's
    language; and the class is a constant the tests pin to qa's value.
    """
    return (str(AI_LABEL_TEXT), AI_BADGE_CLASS)
