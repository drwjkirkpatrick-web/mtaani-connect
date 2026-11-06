"""Prompt 17 acceptance: listings are creatable/listed/filterable/removed;
the 6-photo cap is enforced on the MODEL clean() AND the upload endpoint AND
per attachment (client checks are decoration — PROMPTS.md pitfall);
phone-pattern flags queue a moderation report WITHOUT crashing the save;
and no page ever leaks precise location or contact info (ward-only render).

Cross-app reads go ONLY through public service APIs (media save_upload,
moderation report_content + visible helper) — no other app's internals.
"""
import io
import re

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus, ReportReason
from apps.listings import services
from apps.listings.models import (
    LISTING_CATEGORIES,
    MAX_IMAGES,
    COORDINATE_RE,
    Listing,
    ListingImage,
)
from apps.media.services import save_upload

PUBLISHED = ContentStatus.PUBLISHED.value
REMOVED = ContentStatus.REMOVED.value

# The moderation module's target allow-list is parent-owned; the parent's
# Phase-2 duty (see apps/moderation/models.py comment) is to add "listings"
# to ALLOWED_TARGET_APPS (messaging's twin adds the other one). Tests below
# simulate that one-line change IN PROCESS — no file is ever modified.
def _simulate_allowlist_extension(monkeypatch):
    """Mirror the parent's single edit faithfully: the tuple feeds BOTH the
    clean() gate AND the FK's frozen limit_choices_to dict (built at class
    definition), so an in-process simulation must patch both alike."""
    import apps.moderation.models as moderation_models

    extended = tuple(moderation_models.ALLOWED_TARGET_APPS) + ("listings",)
    monkeypatch.setattr(moderation_models, "ALLOWED_TARGET_APPS", extended)
    field = moderation_models.Report._meta.get_field("target_ct")
    # Django 6 keeps limit_choices_to on remote_field (not the FK itself).
    monkeypatch.setattr(
        field.remote_field, "limit_choices_to", {"app_label__in": extended}
    )


# --- tiny deterministic test photos (no binary blobs in the repo) ------------

def _tiny_jpeg(width=48, height=32) -> bytes:
    img = Image.new("RGB", (width, height), (40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _make_photo(user):
    """One real MediaRef through the Prompt 06 pipeline — never hand-made
    rows (budget/exposure tests must measure the path production uses)."""
    upload = SimpleUploadedFile("p.jpg", _tiny_jpeg(), content_type="image/jpeg")
    return save_upload(file_obj=upload, owner=user, purpose="listing")


GOOD_LISTING = {
    "title": "Jiko la makaa - hali nzuri",
    "description": "Jiko lingine la makaa, linawaka vizuri. Eneo la kuchukua tunapatana msg.",
    "category": "furniture",
    "ward": "Kilimani",
    "price": 1500,
}


@pytest.fixture
def seller(fresh_user):
    return fresh_user


# ---------------------------------------------------------------- detector unit cases


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Call 0712 345 678", ["0712 345 678"]),
        ("Call 0712-345-678", ["0712-345-678"]),
        ("Call +254712345678", ["+254712345678"]),
        ("Call +254 712 345 678", ["+254 712 345 678"]),
        ("0712345678 alisema", ["0712345678"]),
        ("whatsapp 0723-456-789 tu", ["0723-456-789"]),
        ("Simu ya duka 0734 567 890 au 0712 345 678", ["0734 567 890", "0712 345 678"]),
        ("piga simu 0711 222 333 asubuhi", ["0711 222 333"]),
        ("+254 711 222 333 kwa haraka", ["+254 711 222 333"]),
        # the honest NEGATIVES — phone-shaped things that are not numbers:
        ("Tunapatana 07:00 asubuhi", []),
        ("Panga miadi 07:00 kesho", []),
        ("Tarehe 2019-07-15", []),
        ("Bei 7000 tu", []),
        ("SIM kadi ya VIN0712 series", []),
        ("Namba ya post box 0701", []),
        ("Jiko la makaa", []),
        ("", []),
    ],
)
def test_detect_phone_shapes_and_negatives(text, expected):
    """THE pitfall detector: Kenyan mobile shapes match (with separators /
    spaces / both forms), while 07:00 times, dates, prices and token-glued
    digits do NOT (people legitimately discuss phone-shaped things — the
    detector must be precise)."""
    assert services.detect_phone_patterns(text) == expected


@pytest.mark.parametrize(
    "text",
    ["Call 0712 345 678", "+254712345688", "0712 345 678 kwa haraka"],
)
def test_detect_phone_deduplicates_repeats(text):
    assert services.detect_phone_patterns(text) == services.detect_phone_patterns(text)


def test_detector_contract_is_module_level():
    """Prompt 20 consolidates its detector onto this function — document the
    contract in code: pure, module-level, list[str] in / out."""
    assert callable(services.detect_phone_patterns)
    assert services.detect_phone_patterns("x") == []


# ---------------------------------------------------------------- CRUD round-trip


@pytest.mark.django_db
def test_create_round_trip_via_form(client_auth):
    response = client_auth.post(
        "/market/new/",
        {"title": "Kavu ya mzigo", "title_sw": "",
         "description": "Kavu yenye mikanda miwili, hali nzuri.",
         "description_sw": "", "category": "tools", "condition": "good",
         "price": "500", "price_negotiable": "on", "ward": "Makadara"},
    )
    assert response.status_code == 302
    listing = Listing.objects.get()
    assert listing.status == PUBLISHED          # publish-on-create, like posts
    assert listing.seller.username == "wanjiku"
    assert listing.price == 500 and listing.price_negotiable is True
    assert not listing.review_flagged           # clean public text, clean flag
    detail = Client().get(f"/market/{listing.pk}/")
    assert detail.status_code == 200
    assert "Kavu yenye mikanda".encode() in detail.content


@pytest.mark.django_db
def test_sw_title_description_round_trip_via_form(client_auth, fresh_user):
    client_auth.post(
        "/market/new/",
        {"title": "Nguo za kazi", "title_sw": "Nguo za kazi za shambani",
         "description": "Overalls za shambani.", "description_sw": "Zina mifuko mitatu.",
         "category": "new", "condition": "new", "price": "1200",
         "price_negotiable": "", "ward": "Ngara"},
    )
    listing = Listing.objects.get()
    assert listing.title_sw == "Nguo za kazi za shambani"
    assert listing.description_sw == "Zina mifuko mitatu."
    # Kiswahili text is PUBLIC TEXT — the detector covers it too. Detection
    # runs on the create/edit write path (services), not bare clean(): flag
    # + report are SIDE EFFECTS and clean() stays side-effect-free.
    listing2 = services.edit_listing(
        listing=listing,
        editor=fresh_user,
        description_sw="piga simu 0711 222 333 asubuhi",
    )
    assert listing2.review_flagged is True


@pytest.mark.django_db
def test_create_requires_login(client):
    assert client.get("/market/new/").status_code == 302


@pytest.mark.django_db
def test_blank_required_fields_rejected_at_form(client_auth):
    response = client_auth.post(
        "/market/new/",
        {"title": "   ", "description": "", "category": "", "ward": ""},
    )
    assert response.status_code == 200          # form re-render, never a 500
    assert Listing.objects.count() == 0


@pytest.mark.django_db
def test_price_or_negotiable_rule_enforced_on_model(seller):
    listing = Listing(
        seller=seller, title="Hakuna bei, hakuna nbei", description="…",
        category="tools", ward="Kilimani",
    )
    with pytest.raises(ValidationError):
        listing.full_clean()
    listing.price = 100
    listing.full_clean()                        # price present → valid
    listing.price = None
    listing.price_negotiable = True
    listing.full_clean()                        # negotiable present → valid


@pytest.mark.django_db
def test_unknown_category_never_stores(seller):
    with pytest.raises(ValidationError):
        services.create_listing(
            seller=seller, title="X", description="d", category="spaceship",
            ward="Kilimani", price=10,
        )


@pytest.mark.django_db
def test_edit_round_trip_and_seller_gate(client_auth, fresh_user):
    listing = services.create_listing(
        seller=fresh_user, title="Old", description="Old body.",
        category="furniture", ward="Umoja", price=100,
    )
    other = get_user_model().objects.create_user("juma", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    assert (
        client_other.post(
            f"/market/{listing.pk}/edit/",
            {"title": "hacked", "description": "hacked", "category": "furniture",
             "condition": "good", "price": "1", "ward": "Umoja"},
        ).status_code
        == 403
    )
    response = client_auth.post(
        f"/market/{listing.pk}/edit/",
        {"title": "Bei mpya: kito", "title_sw": "", "description": "Kitu kizuri.",
         "description_sw": "", "category": "new", "condition": "likenew",
         "price": "200", "price_negotiable": "", "ward": "Umoja"},
    )
    assert response.status_code == 302
    listing.refresh_from_db()
    assert listing.title == "Bei mpya: kito"
    assert listing.status == PUBLISHED          # status is NOT editable via edit


@pytest.mark.django_db
def test_soft_remove_hides_everywhere_but_keeps_rows(client_auth, fresh_user):
    listing = services.create_listing(
        seller=fresh_user, title="Goodbye", description="Sitauz tena.",
        category="tools", ward="Kilimani", price=10,
    )
    assert client_auth.post(f"/market/{listing.pk}/remove/").status_code == 302
    listing.refresh_from_db()
    assert listing.status == REMOVED
    assert f"/market/{listing.pk}/".encode() not in client_auth.get("/market/").content
    assert Client().get(f"/market/{listing.pk}/").status_code == 404
    own = client_auth.get(f"/market/{listing.pk}/")
    assert own.status_code == 200 and b"Removed" in own.content


@pytest.mark.django_db
def test_draft_is_private_except_seller(client_auth, fresh_user):
    listing = Listing.objects.create(
        seller=fresh_user, title="Draft stock", description="Not yet up.",
        category="electronics", ward="Pangani",
        status=ContentStatus.DRAFT.value,
    )
    assert Client().get(f"/market/{listing.pk}/").status_code == 404
    own = client_auth.get(f"/market/{listing.pk}/")
    assert own.status_code == 200 and b"Draft" in own.content


# ---------------------------------------------------------------- category filter + q


@pytest.mark.django_db
def test_category_filter_chips(fresh_user):
    services.create_listing(
        seller=fresh_user, title="Fresh maize", description="Green mombasa maize.",
        category="farm produce", ward="Kilimani", price=50,
    )
    tools = services.create_listing(
        seller=fresh_user, title="Hammer set", description="Claw hammer.",
        category="tools", ward="Kilimani", price=700,
    )
    page = Client().get("/market/?category=farm+produce")
    entries = list(page.context["listings"])
    assert len(entries) == 1 and entries[0].category == "farm produce"
    assert tools.pk not in [l.pk for l in entries]
    everything = Client().get("/market/")
    assert len(everything.context["listings"]) == 2
    # Chips expose every seeded category — extendable with no migration.
    # WHY the .replace: a space ("farm produce") urlencodes inside the chip
    # href; the label itself is translated, so the VALUE is what we assert.
    body = everything.content.decode()
    for value, _label in LISTING_CATEGORIES:
        assert value.replace(" ", "%20") in body


@pytest.mark.django_db
def test_q_search_hits_titles_en_and_sw(fresh_user):
    hit = services.create_listing(
        seller=fresh_user, title="Mbegu za nyanya", description="Fresh seeds.",
        category="farm produce", ward="Kilimani", price=50,
    )
    miss = services.create_listing(
        seller=fresh_user, title="Old radio", description="Vintage tuner.",
        category="electronics", ward="Kilimani", price=1200,
    )
    page = Client().get("/market/?q=nyanya")
    found = {l.pk for l in page.context["listings"]}
    assert hit.pk in found and miss.pk not in found
    assert Client().get("/market/?q=nonexistent-wallet").content.count(
        b"Nothing on sale"
    ) >= 1


# ---------------------------------------------------------------- 6-image cap, three layers


@pytest.fixture
def photo_listing(seller, db):
    return services.create_listing(
        seller=seller, title="Chairs", description="Six plastic chairs.",
        category="furniture", ward="Kilimani", price=6000,
    )


def _fill_photo_slots(listing, user, n=MAX_IMAGES):
    return [services.add_listing_image(listing, _make_photo(user)) for _ in range(n)]


@pytest.mark.django_db
def test_listing_clean_rejects_more_than_six_photos(seller, photo_listing):
    """THE model gate: even a FUTURE write path that attaches 7 photos
    (admin, API, script) and then full_clean()s the listing fails."""
    for n in range(MAX_IMAGES + 1):
        ListingImage.objects.create(
            listing=photo_listing, image=_make_photo(seller), position=n + 1
        )
    with pytest.raises(ValidationError):
        photo_listing.full_clean()


@pytest.mark.django_db
def test_service_add_image_enforces_cap(seller, photo_listing):
    assert MAX_IMAGES == 6
    for _ in range(MAX_IMAGES):
        services.add_listing_image(photo_listing, _make_photo(seller))
    seventh = _make_photo(seller)
    with pytest.raises(ValidationError):
        services.add_listing_image(photo_listing, seventh)
    assert photo_listing.attachments.count() == MAX_IMAGES


@pytest.mark.django_db
def test_attachment_clean_is_third_cap_layer(seller, photo_listing):
    """Layer 3: a direct bridge row insert (no service, no endpoint) fails
    in ListingImage.clean() when 6 slots are already taken."""
    _fill_photo_slots(photo_listing, seller)
    row = ListingImage(listing=photo_listing, image=_make_photo(seller), position=1)
    with pytest.raises(ValidationError):
        row.full_clean()
    row = ListingImage(listing=photo_listing, image=_make_photo(seller), position=9)
    with pytest.raises(ValidationError):
        row.full_clean()                        # position bound checked too


@pytest.mark.django_db
def test_upload_endpoint_happy_path_orders_slots(seller, client_auth, photo_listing):
    for n in (1, 2):
        response = client_auth.post(
            f"/market/{photo_listing.pk}/images/upload/",
            {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
        )
        assert response.status_code == 201, response.content
        assert response.json()["position"] == n  # slots fill in order
    attachments = list(photo_listing.attachments.order_by("position"))
    assert [r.position for r in attachments] == [1, 2]
    detail = Client().get(f"/market/{photo_listing.pk}/")
    body = detail.content.decode()
    # The detail page renders the REAL pipeline output (standard WebP variant)
    assert "/media/images/listing/" in body and all(
        r.image.standard_path.split("/")[-1] == "standard.webp" for r in attachments
    )


@pytest.mark.django_db
def test_upload_endpoint_enforces_six_photo_cap_server_side(seller, client_auth, photo_listing):
    """THE pitfall test: 6 photos already attached → the endpoint refuses a
    7th even though the CLIENT simply posts another file. Count never moves."""
    _fill_photo_slots(photo_listing, seller)
    response = client_auth.post(
        f"/market/{photo_listing.pk}/images/upload/",
        {"image": SimpleUploadedFile("seventh.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 400
    assert photo_listing.attachments.count() == MAX_IMAGES


@pytest.mark.django_db
def test_upload_endpoint_refuses_other_member(photo_listing, fresh_user):
    other = get_user_model().objects.create_user("nani", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    before = photo_listing.attachments.count()
    response = client_other.post(
        f"/market/{photo_listing.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403
    assert photo_listing.attachments.count() == before


@pytest.mark.django_db
def test_upload_endpoint_refuses_anonymous(photo_listing):
    response = Client().post(
        f"/market/{photo_listing.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_upload_endpoint_rejects_corrupt_and_missing(photo_listing, client_auth):
    corrupt = client_auth.post(
        f"/market/{photo_listing.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", b"not an image" * 20, content_type="image/jpeg")},
    )
    assert corrupt.status_code == 400
    missing = client_auth.post(f"/market/{photo_listing.pk}/images/upload/", {})
    assert missing.status_code == 400
    assert photo_listing.attachments.count() == 0


@pytest.mark.django_db
def test_foreign_photo_ref_rejected(seller, photo_listing):
    """KDPA + correctness: you attach YOUR upload, never someone else's."""
    stranger = get_user_model().objects.create_user("mgeni", password="pw-123456")
    stranger_ref = _make_photo(stranger)
    with pytest.raises(PermissionDenied):
        services.add_listing_image(photo_listing, stranger_ref)


# ---------------------------------------------------------------- phone-pattern flagging


def _phone_listing_text() -> str:
    return "Bei ni 800 tu, mswahili piga simu 0712 345 678 jioni"


@pytest.mark.django_db
def test_flag_sets_review_flagged_and_stays_live(seller):
    """THE pitfall contract: detector match → flag + queue, and the listing
    still PUBLISHED (never auto-block; humans decide)."""
    listing = services.create_listing(
        seller=seller, title="Cables", description=_phone_listing_text(),
        category="electronics", ward="Kilimani", price=800,
    )
    listing.refresh_from_db()
    assert listing.review_flagged is True
    assert listing.status == PUBLISHED          # NOT auto-blocked


@pytest.mark.django_db
def test_flag_creates_report_row_in_queue(seller, monkeypatch):
    """With the moderation allow-list extended to listings (the parent's
    one-line Phase-2 change, simulated in-process): the auto-flag lands an
    OPEN SCAM report on the listing in the queue."""
    _simulate_allowlist_extension(monkeypatch)
    from apps.moderation.models import Report, ReportStatus

    listing = services.create_listing(
        seller=seller, title="Cables", description=_phone_listing_text(),
        category="electronics", ward="Kilimani", price=800,
    )
    report = Report.objects.get()
    assert report.target_ct.app_label == "listings"
    assert report.target_id == listing.pk
    # Honest reason: a phone line inside a price negotiation is the scam
    # pattern — ReportReason.SCAM is the closest vocabulary value.
    assert report.reason == ReportReason.SCAM.value
    assert report.status == ReportStatus.OPEN.value
    assert "0712 345 678" in report.note


@pytest.mark.django_db
def test_flag_survives_allow_list_gaps(seller):
    """Repo-as-committed state ('listings' not yet in the moderation
    allow-list — parent-owned file): the detector still runs, the
    seller-visible flag still shows, and NOTHING crashes on save or edit."""
    listing = services.create_listing(
        seller=seller, title="Cables", description=_phone_listing_text(),
        category="electronics", ward="Kilimani", price=800,
    )
    assert listing.review_flagged is True
    listing2 = services.edit_listing(
        listing=listing,
        editor=seller,
        description="pigeni simu 0733 444 555 sasa",
    )
    assert listing2.review_flagged is True      # still flagged, never crashed


@pytest.mark.django_db
def test_flag_rate_limited_skips_gracefully(seller, monkeypatch):
    """THE graceful-degradation test: quota blown (10/h) → the 11th
    auto-flagged listing still saves, still flagged, just no NEW report row
    this hour (NOTE: queue sees the next one in the rolling window)."""
    import apps.moderation.services as moderation_services

    _simulate_allowlist_extension(monkeypatch)
    from apps.moderation.models import Report
    # WHY patch the limit not the clock: the rolling window counts rows in
    # the last hour; a smaller quota is the same mechanism, less time.
    monkeypatch.setattr(moderation_services, "MAX_REPORTS_PER_HOUR", 1)

    first = services.create_listing(
        seller=seller, title="One", description="Call 0700 111 222",
        category="electronics", ward="Kilimani", price=10,
    )
    assert Report.objects.count() == 1
    second = services.create_listing(
        seller=seller, title="Two", description="Call 0700 111 333",
        category="electronics", ward="Kilimani", price=20,
    )
    second.refresh_from_db()
    assert second.review_flagged is True        # the seller-visible flag never drops
    assert Report.objects.count() == 1          # quota skipped the row
    assert second.status == PUBLISHED           # and the listing never blocked


@pytest.mark.django_db
def test_clean_text_clears_flag_on_edit(seller):
    listing = services.create_listing(
        seller=seller, title="Cables", description=_phone_listing_text(),
        category="electronics", ward="Kilimani", price=800,
    )
    assert listing.review_flagged is True
    listing2 = services.edit_listing(
        listing=listing, editor=seller, description="Bei 800 tu jioni. Sawa?"
    )
    assert listing2.review_flagged is False     # flag reflects CURRENT text


@pytest.mark.django_db
def test_title_phone_alone_also_flags(seller):
    listing = services.create_listing(
        seller=seller, title="Cables +254700 111 222", description="Fine.",
        category="electronics", ward="Kilimani", price=5,
    )
    assert listing.review_flagged is True


# ---------------------------------------------------------------- ward-only exposure


@pytest.mark.django_db
def test_ward_field_is_the_only_public_location(seller):
    listing = services.create_listing(
        seller=seller, title="Radio", description="FM tuner, works well.",
        category="electronics", ward="Kasarani", price=900,
    )
    # The model carries NO precise location anywhere — not null fields, not
    # hidden columns: nothing exists to leak.
    field_names = {f.name for f in Listing._meta.get_fields()}
    forbidden = {"latitude", "longitude", "location", "point", "address",
                 "street", "geo", "geom", "coordinates"}
    assert not (field_names & forbidden)
    assert listing.ward == "Kasarani"


def test_coordinate_shaped_ward_text_refused_anywhere(seller):
    listing = Listing(
        seller=seller, title="Radio", description="FM tuner.",
        category="electronics", ward="-1.2921, 36.817223", price=900,
    )
    with pytest.raises(ValidationError):
        listing.full_clean()
    listing.ward = "36.817223 degrees east of Kasarani"
    with pytest.raises(ValidationError):
        listing.full_clean()
    assert COORDINATE_RE.search("-1.2921, 36.817223")


@pytest.mark.django_db
def test_address_ish_but_not_coordinate_string_still_public_safe(fresh_user):
    """A precise-address-ISH string the detector cannot prove is coordinates
    renders publicly, and the RENDERED page contains no coordinate pair —
    exposure is tested at the render layer where buyers actually meet it."""
    listing = services.create_listing(
        seller=fresh_user, title="Table", description="Wooden table, six seats.",
        category="furniture", ward="Plot 45B Ngong Road area", price=4500,
    )
    detail = Client().get(f"/market/{listing.pk}/").content.decode()
    assert "Plot 45B Ngong Road area" in detail   # public render, as typed
    # WHY a PAIR pattern here, not any decimal: the price renders as
    # "4500.00" and that is money, not geography; the smuggling shape is a
    # comma-separated lat/lon pair ("−1.2921, 36.817223").
    coord_pair = re.compile(r"-?\d{1,3}\.\d{2,}\s*,\s*-?\d{1,3}\.\d{2,}")
    assert coord_pair.findall(detail) == []     # NO coordinates in the template
    assert "36.817" not in detail and "-1.2921" not in detail


@pytest.mark.django_db
def test_detail_page_never_shows_contact_fields(fresh_user):
    """Anonymized view (stranger client): no phone/email/contact leak — the
    ONLY contact affordance is the documented messaging link."""
    listing = services.create_listing(
        seller=fresh_user, title="Radio", description="FM tuner, works well.",
        category="electronics", ward="Kasarani", price=900,
    )
    stranger = Client()
    body = stranger.get(f"/market/{listing.pk}/").content.decode()
    assert "wanjiku@example.com" not in body        # email: never
    assert "mailto:" not in body                    # no mailto affordance
    assert " tel:" not in body and "tel=" not in body
    seller_user = fresh_user
    if getattr(seller_user, "phone", None):
        pytest.fail("seller phone rendered — never store one on listings")
    assert '<a class="btn" href="/chats/?listing=' in body
    # And the enquiry string is the full documented contract.
    assert f'href="/chats/?listing={listing.pk}"' in body


# ---------------------------------------------------------------- budget w/ images


@pytest.mark.django_db
def test_budget_with_six_photo_listing(seller, fresh_user, photo_listing):
    """Adversarial: the accepted 6-image worst case still ≤50 KB HTML —
    images themselves are separate cached requests, HTML is text-weight."""
    _fill_photo_slots(photo_listing, seller)
    detail = Client().get(f"/market/{photo_listing.pk}/").content.decode()
    market = Client().get("/market/").content.decode()
    assert_under_budget(detail, kb=50)
    assert_under_budget(market, kb=50)
    assert detail.count("/media/images/listing/") == MAX_IMAGES
    assert market.count("/media/images/listing/") == 1  # first-photo thumb only


@pytest.mark.django_db
def test_blocked_seller_hidden_from_viewer_lists(fresh_user):
    """Viewer-scoped lists go through moderation's PUBLIC visible helper:
    a seller the viewer blocked vanishes from THEIR market list (policy is
    one shared rule — never listings-local logic)."""
    from apps.moderation.services import block_user

    stranger = get_user_model().objects.create_user("mnaji", password="pw-123456")
    services.create_listing(
        seller=stranger, title="Hidden kettle", description="Electric.",
        category="new", ward="Roysambu", price=2100,
    )
    block_user(blocker=fresh_user, blocked=stranger)
    viewer = Client()
    viewer.force_login(fresh_user)
    assert "Hidden kettle".encode() not in viewer.get("/market/").content
    stranger_page = Client().get("/market/")     # anonymous sees it: public
    assert "Hidden kettle".encode() in stranger_page.content
