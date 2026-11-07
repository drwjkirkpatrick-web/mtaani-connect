"""ledger.py — the per-call cost ledger + the daily budget guard (Prompt 33).

COST UNIT: TOKENS. v1 deliberately keeps NO currency — rate cards differ
per provider/model/deployment (the local Jetson costs electricity, not
billable tokens), so converting to cents would be fabricated precision.
The budget guard counts tokens/day; ``AICall`` rows keep in/out plus
elapsed_ms, so a future rate-card pass can compute money WITHOUT
re-reading the providers.

THE GUARD DEGRADES BEFORE EACH CALL, never after: ``run_call`` checks
``daily_budget_guard`` FIRST and raises ``AiUnavailable`` — which views
render as the honest "AI temporarily unavailable" state (no 500, no fake
answer) — rather than overspending. NOTE: one check per call (not per
token); a single call may overshoot into the margin between check and
log — acceptable margin, documented honestly.

RESET BEHAVIOR: none in v1. Nothing needs resetting — the day filter
rolls forward with ``created_at`` (the boundary is tested); a cron would
only delete evidence. ``AI_DAILY_TOKEN_BUDGET`` is a module constant
(generous default 200_000 tokens/day); the parent may hoist it into a
settings var when a real backend lands.

Provider selection is CONFIG (``settings.AI_PROVIDER_NAME``, default
"mock"), not code — see providers.get_active_provider.
"""
from __future__ import annotations

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import AICall
from .providers import LLMProvider, LLMResult, get_active_provider, get_provider

#: Daily token ceiling across ALL providers, counted from ``AICall`` rows.
#: Generous v1 default (mock mode barely dents it; a small local model is
#: the cheapest real backend for Kenyan data budgets). Module constant —
#: documented here; NOT a settings var in v1 (module-level, like the
#: budget helpers, so the number ships in tests without settings wiring).
AI_DAILY_TOKEN_BUDGET = 200_000


class AiUnavailable(Exception):
    """The ONE honest degraded state for AI features.

    Views catch it and render ``str(exc)`` — never a 500, never a fake
    answer (KDPA/brief §6: label and degrade, never fabricate). Default
    text covers the budget path; run_call raises it with the same text
    for stub/misconfigured providers (config is a deploy choice, the
    member-facing story is identical).
    """

    default_message = _("AI temporarily unavailable — try again soon.")

    def __init__(self, message: str | None = None) -> None:
        self.message = message if message else self.default_message
        super().__init__(self.message)


def _call_sums_for(day_qs) -> dict:
    """Sum in/out token columns over a queryset (single SQL round trip)."""
    return day_qs.aggregate(
        total_in=Sum("tokens_in"),
        total_out=Sum("tokens_out"),
    )


def tokens_used_today(now=None) -> int:
    """Tokens spent TODAY (in + out) across all providers and purposes.

    WHY no provider/purpose argument: the budget is a global daily cap —
    every provider shares the same pool; purpose stays free text on
    ledger rows for forensics, not for budgets.
    """
    day = timezone.localdate(now) if now is not None else timezone.localdate()
    sums = _call_sums_for(AICall.objects.filter(created_at__date=day))
    return (sums["total_in"] or 0) + (sums["total_out"] or 0)


def daily_budget_guard(now=None) -> tuple[int, int]:
    """(used_today, remaining) against ``AI_DAILY_TOKEN_BUDGET``.

    ``remaining`` never goes negative — the honest number a status page
    shows when the day is spent. ``now`` is injectable (injected-clock
    hard rule); ``None`` means now.
    """
    used = tokens_used_today(now=now)
    remaining = max(0, AI_DAILY_TOKEN_BUDGET - used)
    return used, remaining


def available_provider(*, now=None, name: str | None = None) -> LLMProvider:
    """The provider a CONSUMER may use this call — or ``AiUnavailable``.

    Degrades BEFORE the call: if today's tokens already cover the
    budget, this raises and no ``complete`` happens at all (tested).
    ``name`` overrides the configured provider for the call (an explicit
    name still passes the BUDGET gate — the pool is global).
    """
    used, remaining = daily_budget_guard(now=now)
    if remaining <= 0:
        raise AiUnavailable()
    return get_active_provider() if name is None else get_provider(name)


# NOTE: log helpers below are the ONLY writers of AICall rows (the
# ledger's one door; the no-auto-publish contract test pins that bare
# provider.complete() writes nothing, here or anywhere else).


def log_call(result: LLMResult, purpose: str = "") -> AICall:
    """Log ONE ``LLMResult`` to the ledger as ONE row.

    ``purpose`` is the short free-text feature label on the row (e.g.
    "status-probe", later "guide-adapt" from Prompt 34) — truncated to
    the column cap so logging never fails on long input.
    """
    row = AICall(
        provider=result.provider_id,
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        elapsed_ms=result.elapsed_ms,
        purpose=(purpose or "").strip()[:60],
    )
    row.full_clean()  # model gates, not just the form (repo pitfall pattern)
    row.save()
    return row


def log_raw(
    *,
    provider: str,
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_ms: int = 0,
    purpose: str = "",
) -> AICall:
    """Log a call whose result is NOT an ``LLMResult`` (future adapters
    may report dicts or partial failures). Same door, same honesty: the
    row records what the provider SAID it spent."""
    row = AICall(
        provider=provider,
        model=model,
        tokens_in=max(0, int(tokens_in)),
        tokens_out=max(0, int(tokens_out)),
        elapsed_ms=max(0, int(elapsed_ms)),
        purpose=(purpose or "").strip()[:60],
    )
    row.full_clean()
    row.save()
    return row


def run_call(
    system: str,
    messages: list[dict],
    *,
    max_tokens: int = 256,
    purpose: str = "",
    now=None,
    provider: LLMProvider | None = None,
) -> tuple[LLMResult, AICall]:
    """THE consumer door (Prompt 34 uses ONLY this + prompt_text).

    Order matters:
    1. BUDGET FIRST (degrade before the call, never after): an exhausted
       day raises ``AiUnavailable`` and NOTHING is called, NOTHING logged.
    2. Resolve the provider (explicit arg, else the configured one —
       config, not code). An UNKNOWN settings name is a config choice
       too: translated into ``AiUnavailable`` so features degrade to the
       honest state instead of a 500 (the status page surfaces the raw
       registry error for staff instead).
    3. ONE ``complete`` call; a stub selected in settings is translated
       into ``AiUnavailable`` as well (honest degrade, not a 500 —
       direct ``provider.complete`` still raises loudly for tests).
       NOTE: shape errors from a provider (bad messages etc.) are NOT
       translated — a programming error must stay loud.
    4. ONE ledger row via log_call.

    Returns ``(result, row)``. ``complete`` writes NOTHING (the
    no-auto-publish contract): the caller keeps, labels and reviews the
    draft itself.
    """
    used, remaining = daily_budget_guard(now=now)
    if remaining <= 0:
        raise AiUnavailable()
    if provider is None:
        try:
            provider = get_active_provider()
        except ValueError as exc:
            raise AiUnavailable(
                _(
                    "AI temporarily unavailable — the configured provider "
                    "name is not recognized."
                )
            ) from exc
    try:
        result = provider.complete(system, messages, max_tokens=max_tokens)
    except NotImplementedError as exc:
        raise AiUnavailable() from exc
    return result, log_call(result, purpose)
