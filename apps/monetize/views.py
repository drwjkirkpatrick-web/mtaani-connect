"""Monetize views (PROMPTS.md Prompt 38) — staff command surfaces + the
honest SACCO premium stub.

This app owns NO consumer surface for monetization itself: no page here
renders promoted listings or paid features — the promoted door is a PURE
LIBRARY (ratio.interleave + services.search_inline + the stored bilingual
labels) consumed by the search-results seam the PARENT wires (exact patch
points in apps/monetize/README.md; tests/test_listing_search.py's pins stay
intact).

Pages:
- monetize:flags (staff) — master switches + the flag audit trail; flips
  POST to monetize:flag_flip (services stamp provenance + audit rows).
- monetize:merchants (staff) — enrollees with EFFECTIVE feature caps
  rendered as data (flag-aware: registry off → everyone reads FREE caps)
  + tier flip forms (monetize:merchant_tier).
- monetize:sacco/<pk> — visible to that SACCO's admins (fleet's public
  tenancy door) and staff; disabled → honest stub copy; enabled → stub +
  documented payload shape; staff flip forms POST to monetize:sacco_tool_flip.
Gate: authenticated non-staff members get 403 (PermissionDenied);
anonymous visitors get the login redirect.
"""
from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.fleet.services import is_sacco_admin  # fleet PUBLIC tenancy door
from apps.routes.models import Sacco  # the model chain fleet's own FK uses

from . import services
from .features import FEATURE_SETS
from .labels import (
    SACCO_PAYLOAD_SHAPE,
    SACCO_STUB_DISABLED_EN,
    SACCO_STUB_DISABLED_SW,
    SACCO_STUB_ENABLED_EN,
    SACCO_STUB_ENABLED_SW,
)
from .models import (
    FLAG_SACCO_TOOLS,
    FeatureFlag,
    MerchantEnrollment,
    MonetizationAudit,
    SaccoPremiumTool,
)

MSG_FLAG_ON = "Flag on. / Kimewashwa na uongozi."
MSG_FLAG_OFF = "Flag off. / Kimezimwa na uongozi."
MSG_TOOL_ON = "Premium tool enabled. / Zana maalum imewashwa."
MSG_TOOL_OFF = "Premium tool disabled. / Zana maalum imezimwa."
MSG_TIER_PRO = "Merchant set Pro. / Wauzaji sasa daraja Pro."
MSG_TIER_FREE = "Merchant set Free. / Wauzaji sasa daraja Bure."
MSG_NOOP = "Nothing changed — it was already in that state. / Hakuna kilichobadilika."


def _flash_state(request, *, changed: bool, now_on: bool, msg_on: str, msg_off: str) -> None:
    """ONE flash rule for every flip surface: a no-change flip says so
    honestly; a real flip names the new state."""
    if not changed:
        messages.success(request, MSG_NOOP)
    elif now_on:
        messages.success(request, msg_on)
    else:
        messages.success(request, msg_off)


def staff_surface(view):
    """Staff-only gate for THIS app's pages: anonymous → login redirect,
    authenticated non-staff member → 403 (staff command territory, full
    stop — monetization pages are not member pages)."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


@staff_surface
def flag_list(request):
    """Master switches + flag audit trail — the page must read honest even
    on a fresh database (nothing flipped, nothing pointed anywhere)."""
    flags = list(FeatureFlag.objects.order_by("key"))
    audits = list(
        MonetizationAudit.objects.filter(
            target_ct=ContentType.objects.get_for_model(FeatureFlag)
        ).order_by("-created_at")[:15]
    )
    return render(
        request,
        "monetize/flags.html",
        {"flags": flags, "audits": audits},
    )


@staff_surface
@require_POST
def flag_flip(request, pk: int, action: str):
    """POST-only enable/disable of ONE master flag (services writes the
    audit rows; no-change flips land no audit)."""
    if action not in ("enable", "disable"):
        raise Http404
    flag = get_object_or_404(FeatureFlag, pk=pk)
    before = bool(flag.enabled)
    services.flip_flag(flag=flag, actor=request.user, enable=(action == "enable"))
    flag.refresh_from_db()
    _flash_state(
        request,
        changed=bool(flag.enabled) != before,
        now_on=bool(flag.enabled),
        msg_on=MSG_FLAG_ON,
        msg_off=MSG_FLAG_OFF,
    )
    return redirect("monetize:flags")


@staff_surface
def merchant_list(request):
    """Staff lists enrollees; each row shows the tier they are AT plus the
    EFFECTIVE caps (flag-aware so an off registry visibly points at
    nothing), and the registry's own bilingual tier copy."""
    enrollees = []
    for enrollment in MerchantEnrollment.objects.select_related("user").order_by(
        "user__username"
    ):
        enrollees.append(
            {
                "enrollment": enrollment,
                # what WOULD apply right now (off registry → FREE for all)
                "effective": services.effective_merchant_features(enrollment.user),
            }
        )
    return render(
        request,
        "monetize/merchants.html",
        {
            "enrollees": enrollees,
            "feature_sets": sorted(FEATURE_SETS.values(), key=lambda s: s.tier),
        },
    )


@staff_surface
@require_POST
def merchant_tier(request, pk: int):
    """POST-only staff tier flip (free/pro) + audit row on real changes."""
    tier = (request.POST.get("tier") or "").strip().lower()
    if tier not in ("free", "pro"):
        raise Http404
    enrollment = get_object_or_404(MerchantEnrollment, pk=pk)
    services.set_merchant_tier(
        enrollment=enrollment, actor=request.user, pro=(tier == "pro")
    )
    enrollment.refresh_from_db()
    if enrollment.tier == "PRO":
        messages.success(request, MSG_TIER_PRO if tier == "pro" else MSG_NOOP)
    else:
        messages.success(request, MSG_TIER_FREE if tier == "free" else MSG_NOOP)
    return redirect("monetize:merchants")


def sacco_premium(request, pk: int):
    """The SACCO premium page: that SACCO's admins (fleet's tenancy door)
    and staff see the tool registry states. Disabled → the honest
    coming-when-enabled stub; enabled → still a STUB view documenting the
    one payload shape the future door will return (never fabricated data)."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    sacco = get_object_or_404(Sacco, pk=pk)
    if not (request.user.is_staff or is_sacco_admin(request.user, sacco)):
        raise PermissionDenied
    tool = SaccoPremiumTool.objects.filter(sacco=sacco).first()
    master_on = services.is_flag_on(FLAG_SACCO_TOOLS)
    visible_enabled = bool(tool and tool.enabled and master_on)
    audits = services.audit_rows_for(tool) if tool else []
    return render(
        request,
        "monetize/sacco_premium.html",
        {
            "sacco": sacco,
            "tool": tool,
            "master_on": master_on,
            "visible_enabled": visible_enabled,
            "stub_disabled_en": SACCO_STUB_DISABLED_EN,
            "stub_disabled_sw": SACCO_STUB_DISABLED_SW,
            "stub_enabled_en": SACCO_STUB_ENABLED_EN,
            "stub_enabled_sw": SACCO_STUB_ENABLED_SW,
            "payload_doc": services.sacco_payload_doc(),
            "payload_doc_debug": services.payload_doc_text(),
            "payload_shape": SACCO_PAYLOAD_SHAPE,
            "audits": audits,
            "is_staff_view": request.user.is_staff,
        },
    )


@require_POST
def sacco_tool_flip(request, pk: int):
    """POST-only STAFF flip of a registered premium tool (+ audit). The
    page's tenancy gate is separate: a SACCO admin can WATCH this page but
    only platform staff flip it."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not request.user.is_staff:
        raise PermissionDenied
    sacco = get_object_or_404(Sacco, pk=pk)
    action = (request.POST.get("action") or "").strip().lower()
    if action not in ("enable", "disable"):
        raise Http404
    tool = SaccoPremiumTool.objects.filter(sacco=sacco).first()
    if tool is None:
        raise Http404
    before = bool(tool.enabled)
    services.flip_sacco_tool(
        tool=tool, actor=request.user, enable=(action == "enable")
    )
    tool.refresh_from_db()
    _flash_state(
        request,
        changed=bool(tool.enabled) != before,
        now_on=bool(tool.enabled),
        msg_on=MSG_TOOL_ON,
        msg_off=MSG_TOOL_OFF,
    )
    return redirect("monetize:sacco", pk=pk)
