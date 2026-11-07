"""AI ledger models (PROMPTS.md Prompt 33) — one row per provider call.

``AICall`` IS the cost ledger. The v1 cost unit is TOKENS, not currency:
rate cards differ per provider, per model and per deployment (and the
local Jetson burns electricity, not billable tokens), so cents would be
fabricated precision. The daily budget guard counts tokens/day (see
ledger.daily_budget_guard). Rows keep both directions plus elapsed_ms,
so a future rate-card pass can compute money WITHOUT re-reading the
providers.

Day accounting: the guard derives "today" from ``created_at`` (indexed
below) — there is NOTHING to reset: no cron job in v1, the day filter
rolls forward by itself (tested at the day boundary in
tests/test_ai_layer.py). Deleting old ledger rows is a future
decision, never an accounting necessity.

WRITE PATH: rows are created ONLY by ledger.log_call / run_call (one
door — the no-auto-publish contract test pins that ``complete()``
itself writes nothing, here or anywhere else).
"""
from django.conf import settings
from django.core.validators import MaxLengthValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.types import TimestampedModel


class AICall(TimestampedModel):
    """One LLM provider call, logged when it happens.

    NOTE: provider/model are stored as STRINGS on the row (the frozen
    facts of that call) — they do not follow later registry renames.
    """

    # WHY CharField not FK: the ledger is provider-agnostic history; it
    # must survive provider removals and renames without cascade drama.
    provider = models.CharField(_("Provider"), max_length=32)
    model = models.CharField(_("Model"), max_length=64)
    tokens_in = models.PositiveIntegerField(_("Tokens in"), default=0)
    tokens_out = models.PositiveIntegerField(_("Tokens out"), default=0)
    # Wall-clock cost of the call as REPORTED by the provider (mock mode
    # counts a deterministic 0 — MockLLM never reads the clock).
    elapsed_ms = models.PositiveIntegerField(_("Elapsed (ms)"), default=0)
    # Free-text, SHORT feature label: "status-probe", later e.g.
    # "guide-adapt" (Prompt 34). Bounded the same way every prose field
    # in this repo is — mobile data is the user's money.
    purpose = models.CharField(
        _("Purpose"),
        max_length=60,
        blank=True,
        default="",
        help_text=_(
            "Short label of the feature that made the call "
            "(e.g. 'status-probe')."
        ),
    )

    class Meta:
        verbose_name = _("AI call")
        verbose_name_plural = _("AI calls")
        ordering = ["-created_at"]  # newest first — a spend surface reads
        # Index for the daily guard's created_at__date read (see ledger).
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self) -> str:
        purpose = self.purpose or "unlabeled"
        return f"{self.provider}/{self.model} {self.total_tokens}t ({purpose})"

    @property
    def total_tokens(self) -> int:
        """in + out — the number the budget guard spends against."""
        return self.tokens_in + self.tokens_out


# ============================================================================
# Prompt 34 — the "Ask Mtaani" sidecar (assistant state, NOT ledger data).
# ============================================================================
# WHY a sidecar and not a user column on AICall: the Prompt 33 ledger is
# provider-agnostic spend history — user-free by contract (it must survive
# provider renames/removals). AssistantRun carries the member's half of one
# adaptation: who asked with which intake texts, which guides the retrieval
# cited (JSON [{pk, title, safety_category}] — EXACTLY the retrieval top-k,
# tested), the labelled draft + the verbatim safety block it renders with,
# and one boolean community-verification click. It DOUBLES as the per-user
# daily quota counter (assistant.ASSISTANT_DAILY_RUNS_PER_USER counts
# today's rows) — no separate quota table, no counter to reset: the date
# filter rolls forward by itself, exactly like AICall's day accounting.
class AssistantRun(TimestampedModel):
    """One "Ask Mtaani" adaptation run (Prompt 34) — draft-only state.

    Rows exist ONLY for successful ledgered runs (a quota-refused or
    AI-unavailable attempt writes nothing — the honest empty state). The
    row is what the result page + the "Did this work for you?" click
    render from, and its created_at (day filter) IS the per-user quota.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="assistant_runs",
    )
    # The guided intake (Prompt 34: "What do you have? What do you need?").
    # Length bounds restated on the MODEL so any future write path
    # inherits them (the form's max_length is not the last line).
    have_text = models.TextField(
        _("What the member has"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(500)],
    )
    need_text = models.TextField(
        _("What the member needs"),
        blank=True,
        default="",
        validators=[MaxLengthValidator(500)],
    )
    # JSON list of {"pk": int, "title": str, "safety_category": str}.
    # URLs are deliberately NOT stored — renders derive them fresh
    # ({% url 'guides:detail' pk %}) so a stored link can never rot.
    cited = models.JSONField(_("Cited guides"), blank=True, default=list)
    # The labelled LLM draft — DRAFT-ONLY content, always rendered with
    # the AI badge (Prompt 12 contract).
    draft_text = models.TextField(_("Adapted draft"), blank=True, default="")
    # The appended safety-warning block (gated categories): the guides'
    # own warnings VERBATIM — the assistant never drops or overrides one.
    safety_block = models.TextField(_("Safety block"), blank=True, default="")
    gated = models.BooleanField(_("Safety gate applied"), default=False)
    # v1 community verification: ONE click, ONE honest flag, NO
    # aggregation — community stats are Prompt 08's realm and stay there.
    verified_helpful = models.BooleanField(
        _("Marked helpful"), default=False
    )

    class Meta:
        verbose_name = _("Assistant run")
        verbose_name_plural = _("Assistant runs")
        # Newest first — the status meter/counters read "today" from here.
        ordering = ["-created_at"]
        # Index for the per-user daily quota read (user + created_at__date).
        indexes = [models.Index(fields=["user", "created_at"])]

    def __str__(self) -> str:
        gated = "gated" if self.gated else "ungated"
        return f"ask#{self.pk} {self.user} — {len(self.cited)} cited ({gated})"
