"""THE MPESA STUB (Prompt 21) — documented future extension point.

WHAT THIS FILE IS: the adapter interface M-Pesa integration WOULD
implement — and nothing else. It is never instantiated, never networked,
never called by any barter flow in V1. There is no settings wiring, no
client library, no credential handling, no sandbox call. (PROMPTS.md
mock-first rule keeps every test network-free; barter V1 needs NO network
at all — the deal terms are self-reported on purpose.)

WHAT WOULD GO HERE LATER (documented so the future reader starts right):
a settings.MPESA_ADAPTER = "apps.barter.mpesa.<ConcreteAdapter>" value
resolved exactly like apps.core.comms resolves SMS_ADAPTER/EMAIL_ADAPTER
(django-import-string; mock adapter first for tests). V1 ships the
interface only.

WHY the stub exists at all: the app NEVER holds money (brief §3 Module 6
— no custody, no escrow in V1). Members settle the stated cash component
THEMSELVES via M-Pesa; the product's only job is to keep the stated
amount and the negotiation trail honest. Wiring real payments would
change the KDPA/custody story entirely — that decision belongs to a
dedicated human-approved future prompt, not to barter's build.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


class MPesaAdapter:
    """Interface for a future M-Pesa integration over the cash component.

    CONTRACT SKETCH (for the future implementer; NONE of this runs now):
    - ``initiate_settlement_request``: STK-push the stated cash component
      to the counterparty's Till/Paybill — initiated by ONE member, only
      after the deal tip is ACCEPTED (barter's state machine is the gate;
      a payment may never outlive or bypass it).
    - ``settlement_status``: idempotent status query keyed by the
      adapter's own reference (never reuse barter Offer ids as M-Pesa
      refs — the adapter has its own ledger).
    ``credentials`` come from .env via settings at wiring time — never
    hard-coded, never logged (PROMPTS.md secrets rule; the parent owns
    .env.example).
    """

    #: Class-level honesty marker; the wiring check refuses to load a
    #: "real" adapter whose subclass forgot this — payments cannot ship
    #: silently.
    is_stub = True

    def __init__(self, credentials: dict[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError(
            "V1 ships no M-Pesa integration — see apps/barter/mpesa.py "
            "for the documented future extension point. The app never "
            "holds money."
        )

    def initiate_settlement_request(self, *args: Any, **kwargs: Any) -> Any:
        """Draft interface (STK push). NEVER called in V1."""
        raise NotImplementedError  # pragma: no cover

    def settlement_status(self, reference: str) -> Any:  # pragma: no cover
        """Draft interface (status query). NEVER called in V1."""
        raise NotImplementedError


def stated_cash_only(cash_amount: Decimal | None) -> str:
    """Tiny helper used by template copy tests (NOT a payment function).

    Returns the honest phrasing the UI renders next to any stated cash —
    asserting via this keeps the no-custody sentence ONE expression so
    translations stay in step.
    """
    from django.utils.translation import gettext as _t

    return _t(
        "You two settle the cash part yourselves (M-Pesa) — the app never "
        "holds money."
    )
