"""Tests for PROMPTS.md Prompt 38 — monetization hooks OFF by default.

Contract points asserted here (acceptance: monetization is ARMED but
pointed at NOTHING until staff flip flags deliberately):
- flags default-off: a fresh database has every master flag OFF, nobody in
  the enabler seat, and ZERO audit rows (seeding is not a flip);
- promoted labeling: the bilingual disclosure (Promoted / Imeandikwa) is
  STORED on the placement row and ships INSIDE every payload row interleave
  emits — an unlabeled promoted row can never pass the ratio door;
- ratio caps: ≤1 promoted per 6 organic (capacity is structural), promoted
  slots land right after every 6th organic row, never adjacent, never
  rendered with no organic context, inputs never mutated;
- audit rows: one per REAL flip (enable AND disable), actor + at stamped,
  none when nothing flips, immutable afterwards;
- feed-ranking independence: source-scan (no scorer import/mutation vocabu-
  lary anywhere in apps/monetize) AND behavior (rank() output byte-identical
  before/after monetize is exercised);
- tier caps: FREE 8/168h vs PRO 25/24h pure lookups; the master flag gates
  effectiveness (off → EVERY merchant reads FREE — even an enrolled PRO);
- SACCO stub honesty: disabled → coming-when-enabled copy in en+sw; enabled
  → still a stub documenting the payload shape (empty aggregates, no
  fabricated data), with the enabling audit row;
- staff-only surfaces: authenticated members get 403, anonymous a login
  redirect, staff 200; this app structurally has NO promoted-content
  rendering of its own.

NOTE: monetize contains NO gettext markers on purpose (labels.py's I18N
DESIGN note) — the shared sw catalog + drift detector live under locale/,
which this app does not own; bilingual copy ships as stored data instead.
"""
from __future__ import annotations

import importlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from apps.core.testing import assert_under_budget, mock_now
from apps.fleet.services import grant_sacco_admin, is_sacco_admin
from apps.feed.ranking import rank
from apps.listings.services import create_listing, remove_listing
from apps.monetize import features, ratio, services
from apps.monetize.labels import (
    PROMOTED_LABEL_EN,
    PROMOTED_LABEL_SW,
    SACCO_STUB_DISABLED_EN,
    SACCO_STUB_DISABLED_SW,
    SACCO_STUB_ENABLED_EN,
    SACCO_STUB_ENABLED_SW,
    SACCO_TOOL_LABEL_EN,
    SACCO_TOOL_LABEL_SW,
)
from apps.monetize.models import (
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
from apps.routes.models import Sacco

pytestmark = pytest.mark.django_db

GOOD_LISTING = {
    "title": "Custom tailoring - suits",
    "description": "Made to measure. Pickup arranged by in-app message.",
    "category": "handmade",
    "ward": "Kawangware",
    "price": 1500,
}


# ------------------------------------------------------------------ helpers
def _user(username, *, staff=False):
    User = get_user_model()
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="***",
        is_staff=staff,
    )


def _staff(username="armorer"):
    return _user(username, staff=True)


def _listing(username="maker", **extra):
    seller = _user(username)
    return create_listing(seller=seller, **{**GOOD_LISTING, **extra})


def _payload(label_en=PROMOTED_LABEL_EN, label_sw=PROMOTED_LABEL_SW, body="P"):
    """A fully labeled promoted payload row (the spec's payload shape)."""
    return {"promoted_en": label_en, "promoted_sw": label_sw, "body": body}


def _promoted_marker_positions(rows):
    """0-based indices of promoted payload rows (their dict marker key)."""
    return [
        i for i, row in enumerate(rows)
        if isinstance(row, dict) and row.get("promoted_en")
    ]


def _feed_rows():
    """Feed-shaped rows EXACTLY per ranking.py's scoring vocabulary."""
    base = dict(  # noqa: C408 — key order stays explicit for readability
        author_id=7,
        author_ward="Kilimani",
        ref_guide_id=None,
        educational=False,
        author_group_ids=[],
    )
    t0 = mock_now()
    return [
        {**base, "pk": 1, "author_ward": "Umoja", "created_at": t0 - timedelta(days=3)},
        {**base, "pk": 2, "educational": True, "created_at": t0 - timedelta(days=1)},
        {**base, "pk": 3, "ref_guide_id": 12, "created_at": t0},
    ]


_FEED_CONTEXT = {
    "followed_user_ids": [7],
    "viewer_ward": "kilimani ",
    "viewer_group_ids": [4],
}


# ------------------------------------------------------------------- flags
def test_fresh_database_every_flag_is_off_with_no_trace():
    """Prompt 38, flags default-off: seeding is NOT a flip — no enabler,
    no enabled_at, and the audit table is untouched."""
    keys = set(FeatureFlag.objects.values_list("key", flat=True))
    assert keys == {key for key, *_ in services.SEED_FLAGS}
    assert len(keys) == 3
    for flag in FeatureFlag.objects.all():
        assert flag.enabled is False
        assert flag.enabler_id is None
        assert flag.enabled_at is None
    assert FeatureFlag.objects.filter(enabled=True).count() == 0
    assert MonetizationAudit.objects.count() == 0
    assert services.is_flag_on(FLAG_SEARCH_INLINE) is False
    assert services.is_flag_on("never_seeded_key") is False  # absence reads OFF


def test_seed_migration_stays_in_parity_with_the_services_registry():
    """The seed migration carries a LITERAL copy (migrations import no live
    modules). This parity lock keeps registry + migration from drifting."""
    mod = importlib.import_module("apps.monetize.migrations.0002_seed_master_flags")
    assert set(mod.SEEDS) == set(services.SEED_FLAGS)


def test_a_flag_row_cannot_enable_itself_silently():
    """Nothing grants itself — an 'enabled' flag with no recorded WHO/WHEN
    refuses to exist (model clean), no matter who built it."""
    flag = FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE)
    flag.enabled = True
    with pytest.raises(ValidationError):
        flag.full_clean()
    flag.enabler, flag.enabled_at = _staff(), mock_now()
    flag.full_clean()  # provenance complete → storage-worthy
    with pytest.raises(ValidationError):
        MonetizationAudit.objects.update(note="tampered")


def test_flip_guards_require_staff_and_write_nothing_when_refused():
    flag = FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE)
    member = _user("selfserver")
    with pytest.raises(PermissionDenied):
        services.flip_flag(flag=flag, actor=member, enable=True, note="nope")
    with pytest.raises(PermissionDenied):
        services.flip_flag(flag=flag, actor=None, enable=True)
    flag.refresh_from_db()
    assert flag.enabled is False
    assert MonetizationAudit.objects.count() == 0  # refusal wrote nothing


def test_staff_enable_writes_audit_row_with_actor_and_at():
    staff = _staff()
    flag = FeatureFlag.objects.get(key=FLAG_MERCHANT_TIERS)
    enabled = services.flip_flag(
        flag=flag, actor=staff, enable=True, now=mock_now(), note="demand seen"
    )
    assert enabled.enabled is True
    assert enabled.enabler == staff
    assert enabled.enabled_at == mock_now()
    row = MonetizationAudit.objects.get()
    assert row.surface == MonetizationSurface.FLAG.value
    assert row.action == AuditAction.ENABLED.value
    assert row.actor == staff
    assert row.created_at is not None  # the audit's "at"
    assert row.note == "demand seen"
    assert row.target_id == flag.pk
    assert row.target_ct == ContentType.objects.get_for_model(FeatureFlag)


def test_disable_writes_a_second_audit_row_and_clears_mirrors():
    staff = _staff()
    flag = FeatureFlag.objects.get(key=FLAG_SACCO_TOOLS)
    services.flip_flag(flag=flag, actor=staff, enable=True)
    services.flip_flag(flag=flag, actor=staff, enable=False)
    assert flag.enabled is False and flag.enabler_id is None and flag.enabled_at is None
    actions = [row.action for row in MonetizationAudit.objects.all()]
    assert actions == [AuditAction.DISABLED.value, AuditAction.ENABLED.value]


def test_no_change_flips_write_no_audit_row():
    """THE audit-row honesty line: nothing flips → nothing is written."""
    staff = _staff("quietarmorer")
    off_flag = FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE)
    services.flip_flag(flag=off_flag, actor=staff, enable=False)   # already off
    assert MonetizationAudit.objects.count() == 0
    services.flip_flag(flag=off_flag, actor=staff, enable=True)
    assert MonetizationAudit.objects.count() == 1
    services.flip_flag(flag=off_flag, actor=staff, enable=True, now=mock_now())  # noop re-apply
    assert MonetizationAudit.objects.count() == 1


# ------------------------------------------------------- promoted labeling
def test_placement_default_labels_ship_bilingual_copy():
    """The label copy ships INSIDE the placement row (en+sw), spec-given
    defaults — consumers render it, they never invent it."""
    staff = _staff()
    listing = _listing("promotingmaker")
    placement = services.promote_listing(listing=listing, actor=staff)
    assert placement.slot == PlacementSlot.SEARCH_INLINE.value  # v1's one slot
    assert placement.active is False  # registered ≠ enabled
    assert placement.promoted_en == PROMOTED_LABEL_EN == "Promoted"
    assert placement.promoted_sw == PROMOTED_LABEL_SW == "Imeandikwa"
    assert MonetizationAudit.objects.count() == 0  # registration flips nothing
    again = services.promote_listing(listing=listing, actor=staff)
    assert again.pk == placement.pk  # idempotent on (listing, slot)
    assert placement.enabler_id is None and placement.enabled_at is None


def test_placement_enable_writes_an_audit_row():
    staff = _staff()
    placement = services.promote_listing(listing=_listing(), actor=staff)
    services.flip_placement(placement=placement, actor=staff, enable=True)
    row = MonetizationAudit.objects.get()
    assert row.surface == MonetizationSurface.PLACEMENT.value
    assert row.action == AuditAction.ENABLED.value
    assert row.actor == staff
    assert row.target_id == placement.pk


def test_promoted_payloads_carry_stored_bilingual_labels():
    """Every payload row a consumer could render has BOTH labels attached —
    the disclosure travels with the paid row, per spec."""
    staff = _staff()
    listing = _listing("labelmaker")
    placement = services.promote_listing(listing=listing, actor=staff)
    services.flip_placement(placement=placement, actor=staff, enable=True)
    services.flip_flag(
        flag=FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE),
        actor=staff, enable=True,
    )
    payloads = services.build_search_inline_promoted()
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["listing"].pk == listing.pk
    assert payload["slot"] == PlacementSlot.SEARCH_INLINE.value
    assert payload["promoted_en"] == "Promoted"
    assert payload["promoted_sw"] == "Imeandikwa"
    assert payload["promoted"] is True


def test_door_off_by_default_points_at_nothing():
    """Armed-but-pointed-at-nothing, mechanically: even with a placement
    ENABLED, the door is inert until the master flag is deliberately on —
    and while off it is a byte-identical passthrough (same list object)."""
    staff = _staff()
    placement = services.promote_listing(listing=_listing(), actor=staff)
    services.flip_placement(placement=placement, actor=staff, enable=True)
    organic = [{"title": f"organic {i}"} for i in range(1, 8)]  # 7 organic rows
    assert services.build_search_inline_promoted() == []
    served = services.search_inline(organic)
    assert served is organic and served == organic  # off = same list object
    organic.append({"title": "appended by the caller"})

    services.flip_flag(
        flag=FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE),
        actor=staff, enable=True,
    )
    after = services.search_inline(organic)
    assert after is not organic and len(after) == 9  # 1 paid slot beside 8 organic
    assert _promoted_marker_positions(after) == [6]  # right after the 6th organic
    assert len(organic) == 8                          # the organic list untouched (pure)


def test_removed_or_hidden_listings_decay_out_of_promoted_slots():
    """Paid slots read through the listings PUBLIC door: a listing that is
    no longer PUBLISHED drops out of payloads — no special-casing, no
    dangling paid slot."""
    staff = _staff()
    listing = _listing("vanishingmaker")
    placement = services.promote_listing(listing=listing, actor=staff)
    services.flip_placement(placement=placement, actor=staff, enable=True)
    services.flip_flag(
        flag=FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE), actor=staff, enable=True
    )
    assert len(services.build_search_inline_promoted()) == 1
    remove_listing(listing=listing, by_user=listing.seller)
    assert services.build_search_inline_promoted() == []  # honest emptiness


# --------------------------------------------------------------- ratio caps
def test_ratio_seven_organic_one_promoted_slots_after_the_sixth():
    organic = [f"organic-{i}" for i in range(1, 8)]
    out = ratio.interleave(organic, [_payload(body="paid")], ratio=6)
    positions = _promoted_marker_positions(out)
    assert positions == [6]  # 0-based: right after the 6th organic
    organic_ids = [r for r in out if isinstance(r, str)]
    assert organic_ids == organic  # organic order untouched


def test_ratio_capacity_drops_excess_promotion_never_adjacent():
    organic = [f"organic-{i}" for i in range(1, 27)]  # 26 organic
    promoted = [_payload(body=f"paid-{i}") for i in range(1, 6)]  # 5 candidates
    out = ratio.interleave(organic, promoted, ratio=6)
    positions = _promoted_marker_positions(out)
    assert positions == [6, 13, 20, 27]          # capacity 26 // 6 = 4 slots
    assert len(out) == 30                        # 26 organic + 4 promoted
    assert promoted[4]["body"] == "paid-5"
    assert "paid-5" not in json.dumps(out)       # excess dropped, never stacked
    for a, b in zip(positions, positions[1:]):   # never adjacent
        assert b - a > 1


def test_ratio_never_promotes_into_an_empty_stream():
    assert ratio.interleave([], [_payload()]) == []
    assert ratio.interleave([], [_payload(), _payload(), _payload()]) == []


def test_ratio_rejects_unlabeled_and_malformed_promoted_rows():
    organic = ["a", "b", "c", "d", "e", "f"]
    with pytest.raises(ValueError):
        ratio.interleave(organic, [{"body": "paid, no labels"}])
    with pytest.raises(ValueError):
        ratio.interleave(organic, [{"promoted_en": "Promoted"}])  # sw missing
    with pytest.raises(ValueError):
        ratio.interleave(organic, [{"promoted_en": " ", "promoted_sw": " "}])


def test_ratio_rejects_bad_ratio_arguments():
    organic = ["a", "b", "c", "d", "e", "f"]
    for bad in (0, -1, "6", 2.0, True):
        with pytest.raises(ValueError):
            ratio.interleave(organic, [_payload()], ratio=bad)


def test_ratio_is_pure_inputs_are_never_mutated():
    organic = [{"title": f"o{i}"} for i in range(13)]
    promoted = [_payload("Promoted", "Imeandikwa", f"p{i}") for i in range(3)]
    organic_snapshot = json.dumps(organic)
    promoted_snapshot = json.dumps(promoted)
    ratio.interleave(organic, promoted, ratio=6)
    assert json.dumps(organic) == organic_snapshot
    assert json.dumps(promoted) == promoted_snapshot


# ------------------------------------------------------------- feed rank independence
def test_monetize_never_imports_or_mutates_the_scorer_source_scan():
    """THE structural assert: no file under apps/monetize contains a feed
    scorer import or mutation of its vocabulary. (Monetize decorates the
    OUTPUT of a consumer's ordering; it never re-orders anything itself.)"""
    app_root = Path(services.__file__).resolve().parent
    forbidden = ("apps.feed", "feed.ranking", "score_row", "rank(", "usefulness")
    for path in sorted(app_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} mentions {needle!r}"


def test_rank_output_byte_identical_with_monetize_fully_in_play():
    """Behavioral assert: exercising monetize's WHOLE library cannot change
    what the feed's pure scorer returns for given rows (same input → same
    output, byte for byte)."""
    rows = _feed_rows()
    expected = rank(rows, context=_FEED_CONTEXT, now=mock_now())
    expected_json = json.dumps(expected, sort_keys=True, default=str)

    # run the whole monetize library right in between the two rank calls:
    ratio.interleave(_feed_rows(), [_payload()], ratio=6)
    services.is_flag_on(FLAG_SEARCH_INLINE)  # reads tables, touches nothing
    services.effective_merchant_features(None)
    after = rank(rows, context=_FEED_CONTEXT, now=mock_now())
    # WHY default=str: rows carry datetimes; the comparison must be byte
    # equality over the whole structure, not a lossy projection of it.
    assert json.dumps(after, sort_keys=True, default=str) == expected_json


# ---------------------------------------------------------------- tier caps
def test_tier_feature_lookups_are_pure_and_degrade_to_free():
    free = features.features(MerchantTier.FREE.value)
    assert (free.portfolio_size_cap, free.refresh_cadence_hours) == (8, 168)
    pro = features.features(MerchantTier.PRO.value)
    assert (pro.portfolio_size_cap, pro.refresh_cadence_hours) == (25, 24)
    assert features.features(MerchantTier.PRO).tier == "PRO"     # enum member is known
    for tier in ("SILVER", "GOLD", None, 42, object()):
        assert features.features(tier).tier == "FREE"  # unknown degrades, never grants
    assert features.portfolio_size_cap("PRO") == 25
    assert features.refresh_cadence_hours(None) == 168


def test_effective_features_bite_when_the_registry_flag_is_off():
    """Off-by-default in effect: with merchant_tiers OFF, EVERY merchant
    reads the FREE caps — even an enrolled PRO (the flags point at nothing
    until the master switch is deliberately flipped)."""
    staff = _staff()
    merchant = _user("merchant")
    enrollment = services.enroll_merchant(
        user=merchant, actor=staff, pro=True, now=mock_now()
    )
    assert enrollment.tier == MerchantTier.PRO.value
    assert services.effective_merchant_features(merchant).tier == "FREE"
    services.flip_flag(
        flag=FeatureFlag.objects.get(key=FLAG_MERCHANT_TIERS),
        actor=staff, enable=True,
    )
    now_on = services.effective_merchant_features(merchant)
    assert now_on.tier == "PRO"
    assert (now_on.portfolio_size_cap, now_on.refresh_cadence_hours) == (25, 24)
    # an absent enrollment is FREE even with the flag on:
    assert services.effective_merchant_features(_user("unlisted")).tier == "FREE"


def test_merchant_tier_flips_are_audited_staff_actions():
    staff = _staff("tierwalker")
    merchant = _user("tiermerchant")
    services.enroll_merchant(user=merchant, actor=staff, pro=False)   # FREE row
    assert MonetizationAudit.objects.count() == 0  # a FREE grant flips nothing
    services.set_merchant_tier(
        enrollment=MerchantEnrollment.objects.get(user=merchant),
        actor=staff, pro=True, now=mock_now(),
    )
    grant_row = MonetizationAudit.objects.get()
    assert (grant_row.surface, grant_row.action) == (
        MonetizationSurface.TIER.value, AuditAction.ENABLED.value
    )
    services.set_merchant_tier(
        enrollment=MerchantEnrollment.objects.get(user=merchant),
        actor=staff, pro=False,
    )
    grant_row, revert_row = list(
        MonetizationAudit.objects.order_by("id")  # insertion order, tie-proof
    )
    assert grant_row.action == AuditAction.ENABLED.value
    assert revert_row.action == AuditAction.DISABLED.value
    assert revert_row.actor == staff and revert_row.created_at is not None
    same = services.set_merchant_tier(
        enrollment=MerchantEnrollment.objects.get(user=merchant),
        actor=staff, pro=False,
    )
    assert MonetizationAudit.objects.count() == 2  # noop tier flip wrote nothing
    with pytest.raises(PermissionDenied):
        services.enroll_merchant(user=_user("intruder"), actor=merchant, pro=True)


# ----------------------------------------------------------- SACCO premium
def _sacco(name="Mtaa Line SACCO"):
    return Sacco.objects.create(name=name)


def _sacco_with_tool(name="Premium Mtaa SACCO"):
    staff = _staff("toolkeeper")
    sacco = _sacco(name)
    return staff, sacco, services.register_sacco_tool(sacco=sacco, actor=staff)


def test_sacco_tool_registry_is_off_by_default_and_uneaudited_at_registration():
    staff, sacco, tool = _sacco_with_tool("Quiet SACCO")
    assert tool.tool == PremiumTool.SESSION_ANALYTICS.value  # v1's only stub
    assert tool.enabled is False
    assert tool.label_en == SACCO_TOOL_LABEL_EN
    assert tool.label_sw == SACCO_TOOL_LABEL_SW
    assert MonetizationAudit.objects.count() == 0  # registration flips nothing
    again = services.register_sacco_tool(sacco=sacco, actor=staff)
    assert again.pk == tool.pk  # unique (sacco, tool) — one registry row


def test_sacco_payload_doc_is_schema_not_data():
    doc = services.sacco_payload_doc()
    assert doc["tool"] == PremiumTool.SESSION_ANALYTICS.value
    assert doc["sessions_per_day"] == []      # honest emptiness — no fabricated
    assert doc["vehicle_session_hours"] == [] # aggregate rows until the real view


def test_sacco_page_gates_tenancy_and_renders_the_honest_disabled_stub(client, fresh_user):
    staff = _staff("saccostaff")
    other = _user("outsider")
    sacco = _sacco("Gated SACCO")
    grant_sacco_admin(user=fresh_user, sacco=sacco, granter=staff)
    services.register_sacco_tool(sacco=sacco, actor=staff)

    response_anon = client.get(reverse("monetize:sacco", args=[sacco.pk]))
    assert response_anon.status_code == 302  # anonymous → login redirect

    other_client = client
    other_client.force_login(other)
    assert other_client.get(
        reverse("monetize:sacco", args=[sacco.pk])
    ).status_code == 403  # a member that is NOT this SACCO's admin

    client.force_login(fresh_user)
    response = client.get(reverse("monetize:sacco", args=[sacco.pk]))
    assert response.status_code == 200     # this SACCO's admin may SEE it
    body = response.content.decode()
    assert SACCO_STUB_DISABLED_EN in body
    assert SACCO_STUB_DISABLED_SW in body
    assert SACCO_TOOL_LABEL_EN in body and SACCO_TOOL_LABEL_SW in body
    assert SACCO_STUB_ENABLED_EN not in body   # disabled stub ≠ enabled copy
    client.force_login(staff)
    assert client.get(reverse("monetize:sacco", args=[sacco.pk])).status_code == 200


def test_enabled_sacco_tool_is_still_an_honest_stub_with_payload_doc(client, fresh_user):
    staff, sacco, tool = _sacco_with_tool("Enabled SACCO")
    grant_sacco_admin(user=fresh_user, sacco=sacco, granter=staff)

    # The page honours BOTH gates: registry master flag first...
    services.flip_flag(
        flag=FeatureFlag.objects.get(key=FLAG_SACCO_TOOLS), actor=staff, enable=True
    )
    # ...then the per-sacco tool flip:
    services.flip_sacco_tool(tool=tool, actor=staff, enable=True)
    tool_audits = services.audit_rows_for(tool)
    assert [row.action for row in tool_audits] == [AuditAction.ENABLED.value]
    assert tool_audits[0].actor == staff
    noop = services.flip_sacco_tool(tool=tool, actor=staff, enable=True)
    assert len(services.audit_rows_for(tool)) == 1  # already on → no new row

    client.force_login(fresh_user)
    body = client.get(reverse("monetize:sacco", args=[sacco.pk])).content.decode()
    assert SACCO_STUB_ENABLED_EN in body and SACCO_STUB_ENABLED_SW in body
    assert "sessions_per_day" in body              # the DOCUMENTED shape
    assert "generated_at" in body
    assert "vehicle_session_hours" in body
    assert "toolkeeper" in body                    # the enabling audit row w/ actor


def test_sacco_registry_master_flag_gates_the_enabled_view(client, fresh_user):
    staff, sacco, tool = _sacco_with_tool("RegistryGated SACCO")
    services.flip_sacco_tool(tool=tool, actor=staff, enable=True)  # tool ON...
    assert services.is_flag_on(FLAG_SACCO_TOOLS) is False          # ...registry OFF
    grant_sacco_admin(user=fresh_user, sacco=sacco, granter=staff)
    client.force_login(fresh_user)
    body = client.get(reverse("monetize:sacco", args=[sacco.pk])).content.decode()
    assert SACCO_STUB_DISABLED_EN in body          # nothing is active anywhere
    assert "sessions_per_day" not in body


# ------------------------------------------------------------ staff surfaces
def test_monetize_command_pages_are_staff_only(client, fresh_user):
    staff = _staff()
    for name in ("monetize:flags", "monetize:merchants"):
        url = reverse(name)
        assert client.get(url).status_code == 302  # anonymous → login
        client.force_login(fresh_user)
        assert client.get(url).status_code == 403  # authenticated member → 403
        client.force_login(staff)
        assert client.get(url).status_code == 200  # staff ok
        client.logout()


def test_flags_page_renders_honest_off_by_default_state(client, fresh_user):
    _staff()
    client.force_login(_staff("viewer"))
    url = reverse("monetize:flags")
    response = client.get(url)
    body = response.content.decode()
    assert response.status_code == 200
    assert "Off by default" in body               # the acceptance banner
    assert "armed but pointed at nothing" in body
    assert "billing" in body.lower()              # future-prompt honesty copy
    for key, *_ in services.SEED_FLAGS:
        assert key in body
    assert "OFF" in body and "nobody has flipped" in body
    assert "Nothing has ever flipped" in body     # zero audit rows state
    assert_under_budget(body, 50)


def test_merchants_page_lists_enrollees_caps_as_data(client, fresh_user):
    staff = _staff()
    merchant = _user("enrollee")
    services.enroll_merchant(user=merchant, actor=staff, pro=True)
    client.force_login(_staff("enrollviewer"))
    body = client.get(reverse("monetize:merchants")).content.decode()
    assert "enrollee" in body
    assert "portfolio ≤ 8" in body                # FREE caps render as data
    assert "portfolio ≤ 25" in body               # the registry's PRO cap
    assert 'name="tier" value="pro"' in body


def test_flag_flip_route_is_post_only_and_audited(client, fresh_user):
    staff = _staff("flipper")
    flag = FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE)
    url = reverse("monetize:flag_flip", args=[flag.pk, "enable"])
    client.force_login(fresh_user)
    assert client.post(url).status_code == 403    # member cannot POST either
    client.force_login(staff)
    assert client.get(url).status_code == 405     # GET is not a flip
    response = client.post(url)
    assert response.status_code == 302
    flag.refresh_from_db()
    assert flag.enabled is True
    assert MonetizationAudit.objects.count() == 1


# ------------------------------------------------------------- structure
def test_no_monetize_route_or_template_renders_promoted_content():
    """This app owns NO consumer surface for monetization: the promoted
    door is a LIBRARY for the search-results seam the parent wires."""
    from apps.monetize import urls as monetize_urls

    names = {pattern.name for pattern in monetize_urls.urlpatterns}
    assert names == {"flags", "flag_flip", "sacco", "sacco_tool_flip",
                     "merchants", "merchant_tier"}
    app_root = Path(services.__file__).resolve().parent
    for template in sorted((app_root / "templates").rglob("*.html")):
        text = template.read_text(encoding="utf-8")
        assert "listings:detail" not in text  # renders no listing page of its own
        assert "KSh" not in text              # no prices anywhere in monetize


def test_listing_touches_go_through_the_public_door_only():
    """FK import safety: monetize references the listings app by its string
    FK + its PUBLIC services door — never by importing their internals."""
    app_root = Path(services.__file__).resolve().parent
    for path in sorted(app_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "apps.listings.models" not in text     # FK stays a string
        assert "apps.listings import" not in text
        assert ".listings import models" not in text


def test_audit_rows_are_append_only():
    staff = _staff("immutabler")
    flag = FeatureFlag.objects.get(key=FLAG_SEARCH_INLINE)
    services.flip_flag(flag=flag, actor=staff, enable=True, note="original note")
    with pytest.raises(ValidationError):
        MonetizationAudit.objects.update(note="rewritten note")
    with pytest.raises(ValidationError):
        MonetizationAudit.objects.filter(pk=MonetizationAudit.objects.first().pk).delete()
