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
