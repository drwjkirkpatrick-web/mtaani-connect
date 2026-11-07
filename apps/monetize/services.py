"""Monetization write-side + the promoted-search door (PROMPTS.md Prompt 38).

THE CONTRACT (acceptance: armed but pointed at nothing):
- Every flip function requires an actor with is_staff — self-service is
  structurally impossible from any surface (page, admin, tests).
- A flip that would not CHANGE anything writes NO audit row ("no audit when
  nothing flips") and returns the row untouched.
- A real flip stamps the surface's provenance trio AND appends exactly ONE
  MonetizationAudit row (surface, action, actor, target, at, note).
- Nothing here prices, bills, or charges — billing is a future prompt once
  demand is proven (PROMPTS.md Prompt 38; brief §8: don't monetize before
  demand).
- NO gettext markers in this app — see labels.py's I18N DESIGN note (the
  shared sw catalog + drift detector live under locale/, not ours).

THE PARENT SEAM (search results — the ONLY consumer door):
`search_inline(organic_rows)` is the one-call library door the search-
results view composes in (apps/monetize/README.md documents the exact patch
points). Flag off → the organic rows come back unchanged (off = byte-
identical passthrough); flag on → labeled payload slots woven by
ratio.interleave, organic ordering untouched. The feed's scoring (Prompt
11) is never imported here — monetize cannot mutate it;
tests/test_monetize.py pins both the source-scan and the byte-identical
rank-output invariants.
"""
from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.listings.services import get_visible_listing  # listings PUBLIC door

from . import ratio as ratio_module
from .features import MerchantFeatureSet  # noqa: F401 (re-export, typing aid)
from .features import features as tier_features_lookup
from .labels import SACCO_PAYLOAD_SHAPE
from .models import (
    AuditAction,
    FeatureFlag,
    FLAG_MERCHANT_TIERS,
    FLAG_SACCO_TOOLS,
    FLAG_SEARCH_INLINE,
    MerchantEnrollment,
    MerchantTier,
    MonetizationAudit,
    MonetizationSurface,
    PlacementSlot,
    PremiumTool,
    PromotedPlacement,
    SaccoPremiumTool,
)
from .ratio import SLOT_RATIO

__all__ = [
    "FLAG_MERCHANT_TIERS", "FLAG_SACCO_TOOLS", "FLAG_SEARCH_INLINE",
    "SEED_FLAGS", "audit_rows_for", "build_search_inline_promoted",
    "effective_merchant_features", "enroll_merchant", "features",
    "flag_for", "flip_flag", "flip_placement", "flip_sacco_tool",
    "is_flag_on", "placement_payload", "promote_listing",
    "register_sacco_tool", "sacco_payload_doc", "search_inline",
    "seed_flags", "set_merchant_tier",
]

# The master switches, in one registry (key, label en, label sw). The SEED
# migration carries its own literal copy (migrations must not import live
# modules); tests/test_monetize.py asserts the two stay in parity so the
# seeded-off fresh-DB contract and this registry cannot drift apart.
SEED_FLAGS = (
    (
        FLAG_SEARCH_INLINE,
        "Promoted listings inside search results",
        "Matangazo yaliyopandishwa kwenye matokeo ya utafutaji",
    ),
    (
        FLAG_MERCHANT_TIERS,
        "Merchant subscription tiers (features, not grants)",
        "Daraja za ushirika kwa wauzaji (vipimo, si utoaji)",
    ),
    (
        FLAG_SACCO_TOOLS,
        "Premium tools for SACCOs (registry stays stubbed)",
        "Zana maalum kwa magari ya kikundi (bado ni mfano)",
    ),
)

MSG_STAFF_REQUIRED = (
    "Staff action required — monetization never grants itself. "
    "/ Inahitaji kituo cha uongozi."
)


# ------------------------------------------------------------------ flags
def seed_flags() -> None:
    """Idempotently ensure the three master flags exist — ALWAYS OFF. Called
    by the seed migration only; deliberately NOT part of any flip path."""
    for key, label_en, label_sw in SEED_FLAGS:
        FeatureFlag.objects.get_or_create(
            key=key,
            defaults={"label": label_en, "label_sw": label_sw},
        )


def flag_for(key: str) -> FeatureFlag | None:
    return FeatureFlag.objects.filter(key=key).first()


def is_flag_on(key: str) -> bool:
    """A missing flag row reads OFF — absence is the honest default state."""
    flag = flag_for(key)
    return bool(flag and flag.enabled)


def _staff_or_raise(actor) -> None:
    if actor is None or not getattr(actor, "pk", None) or not actor.is_staff:
        raise PermissionDenied(MSG_STAFF_REQUIRED)


def _stamp(now):
    """Injected-clock discipline everywhere a flip writes a WHEN."""
    return now or timezone.now()


def _audit(*, surface: MonetizationSurface, action: AuditAction, actor, target, note: str = "") -> MonetizationAudit:
    """Append ONE audit row. The single audit factory for the whole app."""
    return MonetizationAudit.objects.create(
        surface=surface.value,
        action=action.value,
        actor=actor,
        target=target,
        note=(note or "").strip(),
    )


def flip_flag(*, flag: FeatureFlag, actor, enable: bool, now=None, note: str = "") -> FeatureFlag:
    """Turn a master flag ON/OFF (staff only), then audit. No-change flips
    (flag already in that state) write NO audit row."""
    _staff_or_raise(actor)
    flag.refresh_from_db(fields=["enabled", "enabler", "enabled_at"])
    if bool(flag.enabled) == bool(enable):
        return flag
    stamp = _stamp(now)
    flag.enabled = enable
    if enable:
        flag.enabler, flag.enabled_at = actor, stamp
    else:  # state mirrors are current-truth: off means nobody to display
        flag.enabler, flag.enabled_at = None, None
    flag.save(update_fields=["enabled", "enabler", "enabled_at"])
    _audit(
        surface=MonetizationSurface.FLAG,
        action=AuditAction.ENABLED if enable else AuditAction.DISABLED,
        actor=actor,
        target=flag,
        note=note,
    )
    return flag


# ------------------------------------------------------- promoted placements
def promote_listing(*, listing, actor, now=None, note: str = "") -> PromotedPlacement:
    """Staff registers a listing as promotable (search-inline v1 slot).

    Creates the OFF-by-default placement row with the stored bilingual
    label defaults — registration flips nothing, so (audit contract) it
    writes no audit row; enabling the placement is flip_placement."""
    _staff_or_raise(actor)
    placement, _created = PromotedPlacement.objects.get_or_create(
        listing=listing,
        slot=PlacementSlot.SEARCH_INLINE.value,
    )
    return placement


def flip_placement(*, placement: PromotedPlacement, actor, enable: bool, now=None, note: str = "") -> PromotedPlacement:
    """Staff enables/disables ONE placement (staff only) + audit."""
    _staff_or_raise(actor)
    placement.refresh_from_db(fields=["active", "enabler", "enabled_at"])
    if bool(placement.active) == bool(enable):
        return placement
    stamp = _stamp(now)
    placement.active = enable
    if enable:
        placement.enabler, placement.enabled_at = actor, stamp
    else:
        placement.enabler, placement.enabled_at = None, None
    placement.save(update_fields=["active", "enabler", "enabled_at"])
    _audit(
        surface=MonetizationSurface.PLACEMENT,
        action=AuditAction.ENABLED if enable else AuditAction.DISABLED,
        actor=actor,
        target=placement,
        note=note,
    )
    return placement


def placement_payload(placement: PromotedPlacement) -> dict:
    """The promoted PAYLOAD row a consumer renders — the label copy ships
    INSIDE it (spec). `ratio.interleave` refuses any promoted row missing
    its bilingual label, so a consumer can render the disclosure verbatim."""

    return {
        "promoted": True,
        "slot": placement.slot,
        "placement_id": placement.pk,
        "listing": placement.listing,
        "promoted_en": placement.promoted_en,
        "promoted_sw": placement.promoted_sw,
    }


def build_search_inline_promoted(*, limit: int | None = None, for_viewer=None) -> list[dict]:
    """Payload slots for the search-inline consumer — ONLY when the master
    flag is deliberately ON. Rows: PROMOTED placements (enabled this way),
    ordered by pk (deterministic fair rotation seed for v1). Each row is
    re-read through the listings app's PUBLIC read door
    (get_visible_listing): a listing that is no longer PUBLIC (draft,
    under review, removed) can never stay promoted — paid slots decay into
    honest emptiness with zero special-casing."""
    if not is_flag_on(FLAG_SEARCH_INLINE):
        return []
    rows = (
        PromotedPlacement.objects.filter(
            active=True, slot=PlacementSlot.SEARCH_INLINE.value
        )
        .select_related("listing")
        .order_by("pk")
    )
    if limit is not None:
        rows = rows[:limit]
    payloads: list[dict] = []
    for placement in rows:
        # for_user=None: the read door enforces PUBLISHED-only visibility —
        # draft/under-review/removed rows drop out of every payload here.
        if get_visible_listing(placement.listing_id, None) is None:
            continue
        payloads.append(placement_payload(placement))
    return payloads


def search_inline(
    organic_rows, *, ratio=SLOT_RATIO, limit_promoted: int | None = None
) -> list:
    """THE parent-seam door: consume organic search-result rows, return rows
    with ≤1 promoted per `ratio` woven in — or, with the flag off (the
    default), the organic rows themselves, COMPLETELY unchanged (same
    objects, same order; the off state is a passthrough by construction).

    The consumer keeps its OWN ordering (Prompt 19's relevance/recency;
    Prompt 11's feed score is never touched here) and renders each payload
    row's stored disclosure verbatim; see apps/monetize/README.md."""
    if not is_flag_on(FLAG_SEARCH_INLINE):
        return organic_rows
    payloads = build_search_inline_promoted(limit=limit_promoted)
    if not payloads:
        return organic_rows
    return ratio_module.interleave(list(organic_rows), payloads, ratio=ratio)


# ------------------------------------------------------------- merchant tier
def enroll_merchant(*, user, actor, pro: bool, now=None, note: str = "") -> MerchantEnrollment:
    """Staff enrolls a merchant (staff only) at FREE or PRO. The row
    links user ↔ tier; tier changes beyond the initial grant go through
    set_merchant_tier so the audit row lands."""
    _staff_or_raise(actor)
    enrollment, _created = MerchantEnrollment.objects.get_or_create(user=user)
    return set_merchant_tier(
        enrollment=enrollment, actor=actor, pro=pro, now=now, note=note
    )


def set_merchant_tier(*, enrollment: MerchantEnrollment, actor, pro: bool, now=None, note: str = "") -> MerchantEnrollment:
    """Staff sets a merchant's tier (staff only). Every REAL change writes
    one audit row: PRO grant = ENABLED (TIER), PRO revert = DISABLED. A no-
    change call (tier already the target) writes nothing."""
    _staff_or_raise(actor)
    enrollment.refresh_from_db(fields=["tier", "tier_set_by", "tier_set_at"])
    target_tier = MerchantTier.PRO.value if pro else MerchantTier.FREE.value
    if enrollment.tier == target_tier:
        return enrollment
    stamp = _stamp(now)
    enrollment.tier = target_tier
    if pro:  # PRO grants record their grantor; FREE (the default state) clears
        enrollment.tier_set_by, enrollment.tier_set_at = actor, stamp
    else:
        enrollment.tier_set_by, enrollment.tier_set_at = None, None
    enrollment.save(update_fields=["tier", "tier_set_by", "tier_set_at"])
    _audit(
        surface=MonetizationSurface.TIER,
        action=AuditAction.ENABLED if pro else AuditAction.DISABLED,
        actor=actor,
        target=enrollment,
        note=note,
    )
    return enrollment


def effective_merchant_features(user, *, now=None) -> MerchantFeatureSet:
    """The feature set that would apply to `user` RIGHT NOW. This is where
    off-by-default bites: without the merchant_tiers master flag ON, EVERY
    merchant receives the FREE feature set — even an enrolled PRO one. With
    the flag on, the enrollment's tier applies; an absent enrollment is
    FREE (tiers are flags, not grants)."""
    if not is_flag_on(FLAG_MERCHANT_TIERS):
        return tier_features_lookup(MerchantTier.FREE.value)
    enrollment = MerchantEnrollment.objects.filter(user=user).first()
    return tier_features_lookup(enrollment.tier if enrollment else MerchantTier.FREE.value)


# ------------------------------------------------------------ SACCO premium
def register_sacco_tool(*, sacco, actor) -> SaccoPremiumTool:
    """Staff registers a SACCO's premium tool stub (v1: SESSION_ANALYTICS).
    Registration flips nothing and writes no audit row; enabling the tool is
    flip_sacco_tool."""
    _staff_or_raise(actor)
    tool, _created = SaccoPremiumTool.objects.get_or_create(
        sacco=sacco,
        tool=PremiumTool.SESSION_ANALYTICS.value,
    )
    return tool


def flip_sacco_tool(*, tool: SaccoPremiumTool, actor, enable: bool, now=None, note: str = "") -> SaccoPremiumTool:
    """Staff enables/disables ONE registered premium tool (staff only) +
    audit."""
    _staff_or_raise(actor)
    tool.refresh_from_db(fields=["enabled", "enabler", "enabled_at"])
    if bool(tool.enabled) == bool(enable):
        return tool
    stamp = _stamp(now)
    tool.enabled = enable
    if enable:
        tool.enabler, tool.enabled_at = actor, stamp
    else:
        tool.enabler, tool.enabled_at = None, None
    tool.save(update_fields=["enabled", "enabler", "enabled_at"])
    _audit(
        surface=MonetizationSurface.SACCO_TOOL,
        action=AuditAction.ENABLED if enable else AuditAction.DISABLED,
        actor=actor,
        target=tool,
        note=note,
    )
    return tool


def sacco_payload_doc() -> dict:
    """The documented payload SHAPE the future fleet-analytics door will
    return — a schema doc rendered on the enabled stub page, NEVER data:
    aggregate keys stay EMPTY lists (honest emptiness) so a stub can never
    masquerade as real analytics. The shape mirrors the privacy rule: a
    SACCO analytics payload structurally carries no member identity, only
    day/vehicle aggregates (privacy-safe analytics, Prompt 37's rule)."""
    return {
        "sacco": "<SACCO name>",
        "tool": PremiumTool.SESSION_ANALYTICS.value,
        "window_days": 7,
        "sessions_per_day": [],
        "vehicle_session_hours": [],
        "generated_at": "<UTC ISO-8601, injected clock>",
    }


# ------------------------------------------------------------------ reading
def payload_doc_text() -> str:
    """A one-line, JSON-ish rendering of the documented payload shape for
    the enabled stub page (documentation text, never data)."""
    import json

    return json.dumps(sacco_payload_doc(), ensure_ascii=False, indent=2)


def audit_rows_for(target, *, limit: int = 20) -> list[MonetizationAudit]:
    """Audit rows for a target object (flag/placement/enrollment/tool) —
    newest first; the single audit trail, queried by its generic target."""
    return list(
        MonetizationAudit.objects.filter(
            target_ct=ContentType.objects.get_for_model(type(target)),
            target_id=target.pk,
        ).order_by("-created_at")[:limit]
    )
