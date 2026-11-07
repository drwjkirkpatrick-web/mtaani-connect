"""retention.py — the retention sweep (Prompt 40), inventory-driven.

WHAT: one function, ``sweep(*, now)``, that prunes every retention-declared
surface:

  * expired TRACKING SESSIONS via tracking.services.expire_sessions() and
    aged PING POINTS via tracking.services.prune_pings() — Prompt 27's REAL
    public doors (never a re-implementation: the 72 h floor and the cap
    stamping live in ONE place, the tracking module, and compliance only
    CALLS them);
  * rows whose own lifecycle field says they are past their declared
    retention (see EXPIRY_SPECS: consents, OTPs, recovery tokens, login
    attempts, handled data requests, delivered alerts, assistant runs).

The declared periods come from the INVENTORY (inventory rows with a
retention number); a row with retention None is NEVER swept here — it
follows the account-lifetime / deletion trade-off instead.

WHY ``sweep`` accepts only a clock: retention logic must be testable with
an injected clock (PROMPTS.md hard rule) and identical whether it runs in
a test, in manage.py shell, or from cron (wiring documented for Prompt 42:
``0 * * * *  venv python manage.py shell -c "from apps.compliance.retention
import sweep; sweep(); from apps.tracking.services import ..."` — the
compliance sweep CALLS the tracking doors, so one cron line covers both).

Boundary contract mirrors the sibling sweeps: a row is deleted when its
lifecycle moment + retention <= now (AT exactly the boundary it goes), so
no consumer can ever observe a row the sweep would have deleted.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from django.apps import apps as django_apps
from django.utils import timezone

# (label, model, cutoff_field, retention_days, extra_filter or None)
# NOTE: retention_days values here MUST equal the INVENTORY row for the
# same model — the parity test in tests/test_compliance.py enforces the
# match, so a doc/period drift cannot rot silently.
EXPIRY_SPECS: list[tuple[str, str, str, int, dict | None]] = [
    # Aged consent ledger rows: 7-year legal-obligation horizon (KDPA §32
    # consent proof). NOTE the model's own docstring (Prompt 04) says
    # "never hard-delete" — deferred contract: Prompt 40's inventory IS the
    # retention authority and declares the horizon; nothing is purged
    # before 7 years, and a member's consent proof is never deleted merely
    # because their account was anonymized (the ROW is, only after age-out).
    ("consents", "accounts.ConsentEvent", "created_at", 2555, None),
    # OTP/recovery artifacts: 1-day grace past their hard expiry.
    ("verification_codes", "accounts.VerificationCode", "expires_at", 1, None),
    ("recovery_tokens", "accounts.RecoveryToken", "expires_at", 1, None),
    # Abuse bookkeeping: 90 days is enough lock history.
    ("login_attempts", "accounts.LoginAttempt", "created_at", 90, None),
    # Handled DSAR rows: the paperwork of fulfilled/refused requests, kept
    # 90 days AFTER the decision (OPEN requests are never swept).
    ("data_requests", "compliance.DataRequest", "handled_at", 90, None),
    # Delivered arrival alerts: a transient delivery log.
    ("follow_alerts", "alerts.FollowAlert", "notified_at", 90, None),
    # Assistant drafts: ephemeral aid, not a memory.
    ("assistant_runs", "ai.AssistantRun", "updated_at", 90, None),
]


def _now(now: datetime | None) -> datetime:
    """Injected-clock contract: default = project UTC clock."""
    return now or timezone.now()


def _cutoff(now: datetime, retention_days: int) -> datetime:
    """Rows whose lifecycle moment <= cutoff are past retention."""
    return now - timedelta(days=retention_days)


def sweep(*, now: datetime | None = None) -> dict[str, int]:
    """Prune every retention-declared surface; return the counts.

    Keys of the result (label -> rows removed):
      expired_sessions / pruned_pings  — Prompt 27's real doors;
      consents, verification_codes, recovery_tokens, login_attempts,
      data_requests, follow_alerts, assistant_runs — EXPIRY_SPECS rows.

    NOTE (cron wiring for Prompt 42): one hourly cron line suffices —
      ``from apps.compliance.retention import sweep; sweep()``
    since the tracking prune/expiry doors are invoked HERE. A daily
    schedule is acceptable for consents (7-year horizon); hourly matches
    the 72 h ping floor honestly.
    """
    now = _now(now)
    results: dict[str, int] = {"expired_sessions": 0, "pruned_pings": 0}

    # Prompt 27's doors, called (not copied) — the tracking module stays
    # the single owner of its cap stamping and 72 h floor math.
    from apps.tracking.services import expire_sessions, prune_pings

    results["expired_sessions"] = expire_sessions(now=now)
    results["pruned_pings"] = prune_pings(now=now)

    for label, model_ref, cutoff_field, retention_days, extra in EXPIRY_SPECS:
        model = django_apps.get_model(model_ref)
        qs = model.objects.filter(**{f"{cutoff_field}__lte": _cutoff(now, retention_days)})
        if extra:
            qs = qs.filter(**extra)
        removed, _per_model = qs.delete()
        results[label] = removed
    return results
