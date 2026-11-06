"""Barter signals (Prompt 21) — the deal-completed hook Prompt 22 eats.

WHY a module of its own: signal definitions must be importable by BOTH the
emitter (services) and future receivers (reviews) without importing
services (which imports everything else). One line of vocabulary, one
place.

V1 listeners: none wired. Prompt 22 (Reviews) connects in ITS wave — when
a deal completes, each side of the settlement may write a review.
Listeners must be transaction-cheap and never raise into the deal flow
(see services.complete for the firing contract).
"""
from django.dispatch import Signal

#: Sent after an offer tip reaches COMPLETED (and the whole lineage has
#: been settled the same way). Args: ``offer`` = the completed tip row,
#: ``root`` = the lineage root, ``parties`` = the two member ids.
deal_completed = Signal()
