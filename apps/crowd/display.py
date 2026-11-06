"""display.py — honest rider lines ("3 riders report flooding...", Prompt 30).

The ONLY public rendering of a crowd report's people: a COUNT. Usernames
and reporter identities never appear in any surface (KDPA + task rule) —
this module has no accessor that could leak one. Pluralization is
mechanical (ngettext), so "1 rider reports" vs "2 riders report" can never
disagree between pages.
"""
from __future__ import annotations

from django.utils.translation import ngettext

from .models import ReportType


def rider_line(
    kind: str, count: int, *, stage_name: str, route_name: str | None = None
) -> str:
    """Build the display line from a VALUE + count.

    kind is the persisted ReportType value ("FLOODING", ...) — labels are
    resolved here (translatable). Route name is cited when the surviving
    row carries one, per spec: "3 riders report flooding on Ruiru Rd".
    """
    ctx = {
        "count": int(count),
        "kind": str(ReportType(kind).label),
        "stage": stage_name,
        "route": route_name or "",
    }
    if route_name:
        return ngettext(
            "%(count)s rider reports %(kind)s at %(stage)s on route %(route)s",
            "%(count)s riders report %(kind)s at %(stage)s on route %(route)s",
            int(count),
        ) % ctx
    return ngettext(
        "%(count)s rider reports %(kind)s at %(stage)s",
        "%(count)s riders report %(kind)s at %(stage)s",
        int(count),
    ) % ctx
