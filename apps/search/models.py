"""models.py — SearchDocument, the ONE maintained search index (Prompt 14).

WHY a materialized row-per-entity document table: PROMPTS.md 14's pitfall is
computing the tsvector inline ("a table scan per keystroke"). Instead every
indexed entity (guides, posts, Q&A questions, people) has exactly one row
here, maintained by Postgres TRIGGERS created in THIS app's migration
(migrations/0002_search_triggers.py) — never by save() hooks inside the
other apps (one-agent-per-file isolation; see docstring there).

KDPA guard baked into the shape: people documents are written ONLY from the
profiles public allow-list fields (display_name/username, public bio/skills,
ward label when public) — phone numbers, emails and precise coordinates can
never reach this table because the trigger functions never read them.
"""
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class MaintainedTsVector(models.Field):
    """A Postgres `tsvector` column written ONLY by the search triggers
    (migrations/0002). The ORM never computes or writes it.

    WHY not django.contrib.postgres's SearchVectorField: since Django's
    relocation of that class, using it demands 'django.contrib.postgres'
    in INSTALLED_APPS (check postgres.E005) — settings/* are parent-owned
    files under one-agent-per-file isolation, so the search app defines
    this 6-line column type locally instead.
    """

    description = _("Postgres tsvector (maintained by search triggers)")

    def db_type(self, connection):
        return "tsvector"


class SearchDocument(models.Model):
    """One indexed row per searchable entity.

    Rows are written by Postgres triggers only (see migrations/0002):
    the ORM has NO view/serializer that creates them — search results must
    always be real rows (no fabricated results, PROMPTS.md hard rule).
    """

    class Kind(models.TextChoices):
        GUIDE = "guide", _("Guide")
        POST = "post", _("Post")
        QUESTION = "question", _("Question")
        PERSON = "person", _("Person")

    kind = models.CharField(_("Entity kind"), max_length=16, choices=Kind.choices, db_index=True)
    app_label = models.CharField(_("Source app"), max_length=32, db_index=True)
    label = models.CharField(_("Source model"), max_length=64)
    object_pk = models.CharField(_("Source pk"), max_length=64)

    # Display fields. title_en is the canonical display title; title_sw is
    # empty when the entity has no Kiswahili variant (i18n fallback mirrors
    # guides/qa `.display_title` semantics).
    title_en = models.CharField(_("Title (English)"), max_length=200, blank=True, default="")
    title_sw = models.CharField(_("Title (Kiswahili)"), max_length=200, blank=True, default="")

    # Plain-text haystack for pg_trgm typo tolerance (en + sw content mixed
    # — matching the way qa/guides store both languages in plain columns).
    body = models.TextField(_("Text"), blank=True, default="")

    # Maintained full-text column: weight A = titles, B = body text. The
    # 'english' config lexemes AND 'simple' config lexemes both live here,
    # so one GIN index serves en + sw queries (see migration 0002).
    vector = MaintainedTsVector(  # type: ignore[abstract]
        _("MaintainedSearchVector"), null=True, editable=False
    )

    # Coarse public ward label only (KDPA — never coordinates/phones).
    ward = models.CharField(_("Ward"), max_length=64, blank=True, default="")

    # Entity page URL, captured as a plain string AT TRIGGER TIME — SQL
    # cannot call Django reverse(); URLs here are simple fixed path patterns.
    url = models.CharField(_("Entity URL"), max_length=255, blank=True, default="")

    # Content author, for future "my content in search" filtering and for
    # the trigger-time blocked-author exclusion (SET_NULL: if the author is
    # ever hard-deleted the document remains as long as the content does).
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="search_documents",
        verbose_name=_("Author"),
    )

    # created_at mirrors the SOURCE entity's created_at (set by the trigger)
    # so ranking tie-breaks order by content age, not index time. updated_at
    # is when the trigger last refreshed the row (debugging/freshness).
    created_at = models.DateTimeField(_("Content created at"))
    updated_at = models.DateTimeField(_("Last trigger refresh"), null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # ONE document row per indexed entity — triggers upsert on it.
            models.UniqueConstraint(
                fields=["app_label", "object_pk"], name="uniq_search_app_pk"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - debug helper
        return f"{self.kind}:{self.app_label}.{self.object_pk}"
