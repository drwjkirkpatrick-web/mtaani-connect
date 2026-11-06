"""crew.py — transport-side crew role helpers (Prompt 26, NEW file).

PUBLIC API for the transport prompts that follow (27 tracking sessions,
29 …) — import HERE, not from fleet internals:

    from apps.routes.crew import crew_role_of, is_approved_crew, vehicles_of_crew

    crew_role_of(user)     -> "driver" | "conductor" | None
    is_approved_crew(user) -> bool   (True ⇔ an APPROVED crew seat exists)
    vehicles_of_crew(user) -> ACTIVE vehicles with an APPROVED seat

WHY a facade in routes at all: the transport side needs crew authorization
answers but must not reach into fleet's internal tables (PROMPTS.md hard
rule — cross-app reads go through public query functions). Fleet owns the
approval DATA + the tenant wall; these three delegating functions are the
thin, stable keyhole the transport side uses. Implementation lives in
apps.fleet.services (single source of truth) — imports are inside the
function bodies so neither app import breaks before registry setup.

AUTHORITY VS ACCOUNTABILITY (PROMPTS.md 26 pitfall, in code and tests):
an APPROVED crew seat adds position-publishing authority ONLY — later
wired by Prompt 27's tracking sessions on top of is_approved_crew /
vehicles_of_crew here. It does NOT:
  * mute ordinary-user moderation duties (reports, rate limits, blocks all
    apply to crew exactly as before — pinned in tests/test_fleet.py);
  * imply moderation status (is_moderator stays False for crew users);
  * survive revocation (seat REVOKED ⇒ these helpers return None/False).
Nobody broadcasts position authority without an operator human approving
a different human's request — the two-party gate lives in fleet services.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from django.db.models import QuerySet


def crew_role_of(user) -> str | None:
    """'driver' | 'conductor' | None from the latest APPROVED crew seat."""
    from apps.fleet import services as fleet_services

    return fleet_services.crew_role_of(user)


def is_approved_crew(user) -> bool:
    """True only while the user holds an APPROVED seat — never before it."""
    from apps.fleet import services as fleet_services

    return fleet_services.is_approved_crew(user)


def vehicles_of_crew(user) -> "QuerySet":
    """ACTIVE vehicles with the user's APPROVED seat (tracking input)."""
    from apps.fleet import services as fleet_services

    return fleet_services.vehicles_of_crew(user)
