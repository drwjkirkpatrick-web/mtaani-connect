"""Prompt 18 acceptance: services are creatable/listable/filterable/removed
DISTINCT from goods (separate table + routes, same member); the rate-type
matrix is enforced (fixed/hourly need an amount, negotiable forbids one);
the 8-image portfolio cap is enforced on the MODEL clean() AND the upload
endpoint AND per attachment (client checks are decoration — PROMPTS.md
pitfall); and the provider profile shows portfolio + rates without goods
noise. The Prompt 22 reviews hook degrades to an honest 'coming soon'
while the reviews app is absent or lands empty.

Cross-app reads go ONLY through public service APIs (media save_upload,
moderation visible helper, profiles public_data, listings' shared KDPA
regex) — no other app's internals are written.
"""
import io
import sys

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus, ListingKind
from apps.services import services
from apps.services.models import (
    CATEGORY_VALUES,
    SERVICE_CATEGORIES,
    MAX_IMAGES,
    COORDINATE_RE,
    RateType,
    ServiceImage,
    ServiceListing,
)
from apps.media.services import save_upload

PUBLISHED = ContentStatus.PUBLISHED.value
REMOVED = ContentStatus.REMOVED.value


# --- tiny deterministic test photos (no binary blobs in the repo) ------------

def _tiny_jpeg(width=48, height=32) -> bytes:
    img = Image.new("RGB", (width, height), (120, 40, 180))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _make_photo(user):
    """One real MediaRef through the Prompt 06 pipeline — never hand-made
    rows (budget/exposure tests must measure the path production uses).
    NOTE: media's purpose allow-list has no 'service' yet (parent-owned);
    'listing' is the deliberate reuse, documented in the upload view."""
    upload = SimpleUploadedFile("p.jpg", _tiny_jpeg(), content_type="image/jpeg")
    return save_upload(file_obj=upload, owner=user, purpose="listing")


SERVICE = {
    "title": "Somewa fomu 3 na 4 hadi jioni",
    "description": "Somewa hesabu na physics, kila siku jioni 5-8pm.",
    "category": "tutoring",
    "ward": "Kawangware",
    "rate_type": RateType.HOURLY,
    "rate_amount": 500,
}


# ---------------------------------------------------------------- CRUD distinct from goods


@pytest.mark.django_db
def test_service_and_goods_by_same_member_both_live_separate(client_auth, fresh_user):
    """THE distinctness test: one member posts a service AND a goods
    listing; both live, in SEPARATE tables, on SEPARATE route spaces, and
    the goods route renders goods while the services route renders the
    service (kind value = ListingKind.SERVICE, stored as vocabulary)."""
    from apps.listings.services import create_listing

    service = services.create_service(provider=fresh_user, **SERVICE)
    goods = create_listing(
        seller=fresh_user, title="Nguo za harusi",
        description="Ngoma nzuri za harusi.", category="handmade",
        ward="Kawangware", price=8000,
    )
    assert ServiceListing.objects.count() == 1
    from apps.listings.models import Listing

    assert Listing.objects.count() == 1
    assert service.kind == ListingKind.SERVICE.value
    assert ServiceListing.objects.filter(kind=ListingKind.SERVICE.value).exists()
    # Separate pk spaces? No — DIFFERENT TABLES, so each row keeps its own
    # pk; the route spaces are what keep them apart in URLs:
    svc_detail = Client().get(f"/services/{service.pk}/")
    assert svc_detail.status_code == 200
    assert "Somewa fomu 3".encode() in svc_detail.content
    mk_detail = Client().get(f"/market/{goods.pk}/")
    assert mk_detail.status_code == 200
    assert "Nguo za harusi".encode() in mk_detail.content


@pytest.mark.django_db
def test_kind_is_always_pinned_to_service(fresh_user):
    """No write path can store a non-SERVICE kind on a ServiceListing:
    clean() pins the vocabulary even when a caller mangles the field."""
    service = services.create_service(provider=fresh_user, **SERVICE)
    service.kind = "GOODS"
    service.full_clean()                        # pins kind back in clean()
    assert service.kind == ListingKind.SERVICE.value


@pytest.mark.django_db
def test_create_round_trip_via_form(client_auth):
    response = client_auth.post(
        "/services/new/",
        {"title": "Fundi simu", "title_sw": "",
         "description": "Narekebisha simu na betri kwa bei nafaa.",
         "description_sw": "", "category": "repairs",
         "rate_type": "FIXED", "rate_amount": "1200",
         "availability_notes": "Jumatatu hadi Jumamosi, 9am-6pm",
         "ward": "Pangani"},
    )
    assert response.status_code == 302
    service = ServiceListing.objects.get()
    assert service.status == PUBLISHED          # publish-on-create, like listings
    assert service.provider.username == "wanjiku"
    assert service.rate_type == RateType.FIXED and service.rate_amount == 1200
    detail = Client().get(f"/services/{service.pk}/")
    assert detail.status_code == 200
    assert "Narekebisha simu".encode() in detail.content


@pytest.mark.django_db
def test_create_requires_login(client):
    assert client.get("/services/new/").status_code == 302


@pytest.mark.django_db
def test_blank_required_fields_rejected_at_form(client_auth):
    response = client_auth.post(
        "/services/new/", {"title": "   ", "description": "", "category": "", "ward": ""},
    )
    assert response.status_code == 200          # form re-render, never a 500
    assert ServiceListing.objects.count() == 0


@pytest.mark.django_db
def test_unknown_category_never_stores(fresh_user):
    with pytest.raises(ValidationError):
        services.create_service(
            provider=fresh_user, title="X", description="d",
            category="spacewalking", ward="Kilimani",
            rate_type=RateType.NEGOTIABLE,
        )
    assert set(v for v, _ in SERVICE_CATEGORIES) == CATEGORY_VALUES


@pytest.mark.django_db
def test_edit_round_trip_and_provider_gate(client_auth, fresh_user):
    service = services.create_service(provider=fresh_user, **SERVICE)
    other = get_user_model().objects.create_user("juma", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    assert (
        client_other.post(
            f"/services/{service.pk}/edit/",
            {"title": "hacked", "description": "hacked", "category": "repairs",
             "rate_type": "FIXED", "rate_amount": "1", "ward": "Umoja",
             "availability_notes": ""},
        ).status_code
        == 403
    )
    response = client_auth.post(
        f"/services/{service.pk}/edit/",
        {"title": "Somewa mpaka Form 6", "title_sw": "", "description": "Hesabu physics.",
         "description_sw": "", "category": "tutoring", "rate_type": "HOURLY",
         "rate_amount": "600", "availability_notes": "Wikendi tu",
         "ward": "Kawangware"},
    )
    assert response.status_code == 302
    service.refresh_from_db()
    assert service.title == "Somewa mpaka Form 6"
    assert service.status == PUBLISHED          # status is NOT editable via edit
    assert service.rate_amount == 600           # matrix rechecked on edited row


@pytest.mark.django_db
def test_soft_remove_hides_everywhere_but_keeps_rows(client_auth, fresh_user):
    service = services.create_service(provider=fresh_user, **SERVICE)
    assert client_auth.post(f"/services/{service.pk}/remove/").status_code == 302
    service.refresh_from_db()
    assert service.status == REMOVED
    assert f"/services/{service.pk}/".encode() not in client_auth.get("/services/").content
    assert Client().get(f"/services/{service.pk}/").status_code == 404
    own = client_auth.get(f"/services/{service.pk}/")
    assert own.status_code == 200 and b"Removed" in own.content


@pytest.mark.django_db
def test_draft_is_private_except_provider(client_auth, fresh_user):
    service = ServiceListing.objects.create(
        provider=fresh_user, title="Draft skill", description="Not yet up.",
        category="catering", ward="Pangani", rate_type=RateType.NEGOTIABLE,
        status=ContentStatus.DRAFT.value,
    )
    assert Client().get(f"/services/{service.pk}/").status_code == 404
    own = client_auth.get(f"/services/{service.pk}/")
    assert own.status_code == 200 and b"Draft" in own.content


# ---------------------------------------------------------------- rate-type validation matrix


@pytest.mark.parametrize(
    "rate_type, amount, valid",
    [
        (RateType.FIXED, 800, True),
        (RateType.HOURLY, 300, True),
        (RateType.NEGOTIABLE, None, True),
        (RateType.FIXED, None, False),        # fixed REQUIRES amount
        (RateType.HOURLY, None, False),       # hourly REQUIRES amount
        (RateType.NEGOTIABLE, 500, False),    # negotiable FORBIDS amount
    ],
)
def test_rate_type_matrix(fresh_user, rate_type, amount, valid):
    service = ServiceListing(
        provider=fresh_user, title="T", description="d",
        category="repairs", ward="Pangani",
        rate_type=rate_type, rate_amount=amount,
    )
    if valid:
        service.full_clean()                    # no ValidationError
    else:
        with pytest.raises(ValidationError):
            service.full_clean()


@pytest.mark.django_db
def test_negotiable_amount_cleared_on_switch_and_vice_versa(fresh_user):
    """Editing the rate TYPE re-checks the NEW state: switching to
    negotiable with a stale amount fails, and removing the amount with a
    stale fixed type also fails (the matrix is re-cleaned whole)."""
    service = services.create_service(provider=fresh_user, **SERVICE)
    service.rate_type = RateType.NEGOTIABLE
    with pytest.raises(ValidationError):        # stale amount refused
        service.full_clean()
    service.rate_amount = None
    service.full_clean()
    service.save()
    service.rate_type = RateType.HOURLY
    with pytest.raises(ValidationError):        # no amount, wants money
        service.full_clean()


@pytest.mark.django_db
def test_rate_amount_never_negative(fresh_user):
    service = ServiceListing(
        provider=fresh_user, title="T", description="d", category="repairs",
        ward="Pangani", rate_type=RateType.FIXED, rate_amount=-1,
    )
    with pytest.raises(ValidationError):
        service.full_clean()


# ---------------------------------------------------------------- availability notes ≤500


@pytest.mark.django_db
def test_availability_notes_bounded_at_500(fresh_user):
    ok = services.create_service(
        provider=fresh_user, title="T", description="d", category="catering",
        ward="Pangani", rate_type=RateType.NEGOTIABLE,
        availability_notes="x" * 500,
    )
    assert len(ok.availability_notes) == 500
    with pytest.raises(ValidationError):
        services.create_service(
            provider=fresh_user, title="T2", description="d",
            category="catering", ward="Pangani",
            rate_type=RateType.NEGOTIABLE,
            availability_notes="y" * 501,
        )


# ---------------------------------------------------------------- ward-only exposure


@pytest.mark.django_db
def test_ward_field_is_the_only_public_location(fresh_user):
    service = services.create_service(provider=fresh_user, **SERVICE)
    field_names = {f.name for f in ServiceListing._meta.get_fields()}
    forbidden = {"latitude", "longitude", "location", "point", "address",
                 "street", "geo", "geom", "coordinates", "phone", "contact"}
    assert not (field_names & forbidden)
    assert service.ward == "Kawangware"


def test_coordinate_shaped_ward_text_refused(fresh_user):
    service = ServiceListing(
        provider=fresh_user, title="T", description="d", category="repairs",
        ward="-1.2921, 36.817223", rate_type=RateType.NEGOTIABLE,
    )
    with pytest.raises(ValidationError):
        service.full_clean()
    assert COORDINATE_RE.search("-1.2921, 36.817223")


# ---------------------------------------------------------------- category + provider filters


@pytest.mark.django_db
def test_category_filter_chips(fresh_user):
    tutoring = services.create_service(
        provider=fresh_user, title="Form 2 maths", description="Algebra.",
        category="tutoring", ward="Kawangware",
        rate_type=RateType.HOURLY, rate_amount=400,
    )
    repairs = services.create_service(
        provider=fresh_user, title="Screen fix", description="Phone screens.",
        category="repairs", ward="Kawangware",
        rate_type=RateType.FIXED, rate_amount=1500,
    )
    page = Client().get("/services/?category=tutoring")
    entries = list(page.context["services"])
    assert len(entries) == 1 and entries[0].category == "tutoring"
    assert repairs.pk not in [s.pk for s in entries]
    everything = Client().get("/services/")
    assert len(everything.context["services"]) == 2
    # Chips expose every seeded skill category — extendable, no migration.
    body = everything.content.decode()
    for value, _label in SERVICE_CATEGORIES:
        assert value in body


@pytest.mark.django_db
def test_provider_filter_and_q_search(fresh_user):
    tailoring = services.create_service(
        provider=fresh_user, title="Nzuri tailoring", description="Shirts.",
        category="tailoring", ward="Umoja", rate_type=RateType.NEGOTIABLE,
    )
    stranger = get_user_model().objects.create_user("mtumia", password="pw-123456")
    hidden = services.create_service(
        provider=stranger, title="Other tailoring", description="Dresses.",
        category="tailoring", ward="Kilimani", rate_type=RateType.NEGOTIABLE,
    )
    page = Client().get(f"/services/?provider={fresh_user.pk}")
    found = {s.pk for s in page.context["services"]}
    assert tailoring.pk in found and hidden.pk not in found
    q_page = Client().get("/services/?q=tailoring")
    assert len(q_page.context["services"]) == 2      # q ignores provider filter
    bad = Client().get("/services/?provider=dodgy&category=zzz")   # no 500s
    # Unknown filter values fall back to All (the listings precedent) —
    # a stray querystring never hides the whole market silently.
    assert bad.status_code == 200 and len(bad.context["services"]) == 2


# ---------------------------------------------------------------- 8-image cap, three layers


@pytest.fixture
def service_by_provider(fresh_user, db):
    return services.create_service(provider=fresh_user, **SERVICE)


def _fill_portfolio(service, user, n=MAX_IMAGES):
    return [services.add_service_image(service, _make_photo(user)) for _ in range(n)]


@pytest.mark.django_db
def test_service_clean_rejects_more_than_eight_images(fresh_user, service_by_provider):
    """THE model gate: even a FUTURE write path that attaches 9 images
    (admin, API, script) and then full_clean()s the service fails."""
    for n in range(MAX_IMAGES + 1):
        ServiceImage.objects.create(
            service=service_by_provider, image=_make_photo(fresh_user), position=n + 1
        )
    with pytest.raises(ValidationError):
        service_by_provider.full_clean()


@pytest.mark.django_db
def test_service_add_image_enforces_cap(fresh_user, service_by_provider):
    assert MAX_IMAGES == 8                   # portfolio, one more than goods' 6
    for _ in range(MAX_IMAGES):
        services.add_service_image(service_by_provider, _make_photo(fresh_user))
    ninth = _make_photo(fresh_user)
    with pytest.raises(ValidationError):
        services.add_service_image(service_by_provider, ninth)
    assert service_by_provider.attachments.count() == MAX_IMAGES


@pytest.mark.django_db
def test_attachment_clean_is_third_cap_layer(fresh_user, service_by_provider):
    """Layer 3: a direct bridge row insert (no service, no endpoint) fails
    in ServiceImage.clean() when 8 slots are already taken."""
    _fill_portfolio(service_by_provider, fresh_user)
    row = ServiceImage(service=service_by_provider, image=_make_photo(fresh_user), position=1)
    with pytest.raises(ValidationError):
        row.full_clean()
    row = ServiceImage(service=service_by_provider, image=_make_photo(fresh_user), position=9)
    with pytest.raises(ValidationError):
        row.full_clean()                        # position bound checked too


@pytest.mark.django_db
def test_foreign_image_ref_rejected(fresh_user, service_by_provider):
    """KDPA + correctness: you attach YOUR upload, never someone else's."""
    stranger = get_user_model().objects.create_user("mgeni", password="pw-123456")
    stranger_ref = _make_photo(stranger)
    with pytest.raises(PermissionDenied):
        services.add_service_image(service_by_provider, stranger_ref)


@pytest.mark.django_db
def test_upload_endpoint_happy_path_orders_slots(fresh_user, client_auth, service_by_provider):
    for n in (1, 2):
        response = client_auth.post(
            f"/services/{service_by_provider.pk}/images/upload/",
            {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
        )
        assert response.status_code == 201, response.content
        assert response.json()["position"] == n  # slots fill in order
    attachments = list(service_by_provider.attachments.order_by("position"))
    assert [r.position for r in attachments] == [1, 2]
    detail = Client().get(f"/services/{service_by_provider.pk}/")
    body = detail.content.decode()
    # The detail page renders the REAL pipeline output (standard WebP variant)
    assert "/media/images/listing/" in body and all(
        r.image.standard_path.split("/")[-1] == "standard.webp" for r in attachments
    )


@pytest.mark.django_db
def test_upload_endpoint_enforces_eight_image_cap_server_side(fresh_user, client_auth, service_by_provider):
    """THE pitfall test: 8 images already attached → the endpoint refuses a
    9th although the CLIENT simply posts another file. Count never moves."""
    _fill_portfolio(service_by_provider, fresh_user)
    response = client_auth.post(
        f"/services/{service_by_provider.pk}/images/upload/",
        {"image": SimpleUploadedFile("ninth.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 400
    assert service_by_provider.attachments.count() == MAX_IMAGES


@pytest.mark.django_db
def test_upload_endpoint_refuses_other_member(service_by_provider, fresh_user):
    other = get_user_model().objects.create_user("nani", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    before = service_by_provider.attachments.count()
    response = client_other.post(
        f"/services/{service_by_provider.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403
    assert service_by_provider.attachments.count() == before


@pytest.mark.django_db
def test_upload_endpoint_refuses_anonymous(service_by_provider):
    response = Client().post(
        f"/services/{service_by_provider.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_upload_endpoint_rejects_corrupt_and_missing(service_by_provider, client_auth):
    corrupt = client_auth.post(
        f"/services/{service_by_provider.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", b"not an image" * 20, content_type="image/jpeg")},
    )
    assert corrupt.status_code == 400
    missing = client_auth.post(f"/services/{service_by_provider.pk}/images/upload/", {})
    assert missing.status_code == 400
    assert service_by_provider.attachments.count() == 0


@pytest.mark.django_db
def test_upload_endpoint_404_on_unknown_service(client_auth):
    response = client_auth.post(
        "/services/999999/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- budget w/ 8 images


@pytest.mark.django_db
def test_budget_with_eight_image_portfolio(fresh_user, service_by_provider):
    """Adversarial: the accepted 8-image worst case still ≤50 KB HTML —
    images themselves are separate cached requests, HTML is text-weight."""
    _fill_portfolio(service_by_provider, fresh_user)
    detail = Client().get(f"/services/{service_by_provider.pk}/").content.decode()
    listing_page = Client().get("/services/").content.decode()
    assert_under_budget(detail, kb=50)
    assert_under_budget(listing_page, kb=50)
    assert detail.count("/media/images/listing/") == MAX_IMAGES
    # List page: FIRST-portfolio-image thumb only (a skim, not a grid).
    assert listing_page.count("/media/images/listing/") == 1


# ---------------------------------------------------------------- provider profile page


@pytest.mark.django_db
def test_provider_profile_shows_portfolio_and_rates_no_goods_noise(client_auth, fresh_user):
    """THE acceptance test: the provider page shows portfolio + rates — and
    NOTHING from the goods side (this provider also sells goods; the goods
    listing title and /market/ links must never appear on this page)."""
    from apps.listings.services import create_listing

    service = services.create_service(provider=fresh_user, **SERVICE)
    _fill_portfolio(service, fresh_user)  # 8 portfolio images
    # A goods listing on the SAME member — the potential noise source:
    create_listing(
        seller=fresh_user, title="Hekaya ya zamani",
        description="Old kettle.", category="electronics",
        ward="Kawangware", price=10,
    )
    page = Client().get(f"/services/provider/{fresh_user.pk}/")
    body = page.content.decode()
    assert page.status_code == 200
    assert "Somewa fomu 3" in body                    # rates-bearing service present
    assert "wanjiku" in body                          # provider identity + link
    assert body.count("/media/images/listing/") == 8  # portfolio grid shown
    assert "Hekaya ya zamani" not in body             # NO goods noise
    # The SHARED tabbar carries a /market/ chip for the whole site (chrome,
    # not content) — what must never appear is a GOODS LISTING link/title.
    import re

    assert not re.findall(r'href="/market/\d+', body)
    assert " tel:" not in body and "mailto:" not in body   # contact stays in-app


# ---------------------------------------------------------------- enquiry contract + moderation visibility


@pytest.mark.django_db
def test_detail_page_enquiry_contract_never_contact_fields(fresh_user):
    """The ONLY contact affordance is the documented /chats/?service=<pk>
    link (services pk space) — no mailto, no tel, no email leak."""
    service = services.create_service(provider=fresh_user, **SERVICE)
    body = Client().get(f"/services/{service.pk}/").content.decode()
    assert f'href="/chats/?service={service.pk}"' in body
    assert "mailto:" not in body
    assert " tel:" not in body and "tel=" not in body
    assert "wanjiku@example.com" not in body


@pytest.mark.django_db
def test_blocked_provider_hidden_from_viewer_lists(fresh_user):
    """Viewer-scoped lists go through moderation's PUBLIC visible helper:
    a provider the viewer blocked vanishes from THEIR services list —
    one shared policy, never services-local block logic."""
    from apps.moderation.services import block_user

    stranger = get_user_model().objects.create_user("mnaji", password="pw-123456")
    services.create_service(
        provider=stranger, title="Hidden plumbing fix", description="Leaks.",
        category="repairs", ward="Roysambu", rate_type=RateType.FIXED,
        rate_amount=800,
    )
    block_user(blocker=fresh_user, blocked=stranger)
    viewer = Client()
    viewer.force_login(fresh_user)
    assert "Hidden plumbing fix".encode() not in viewer.get("/services/").content
    stranger_page = Client().get("/services/")   # anonymous sees it: public
    assert "Hidden plumbing fix".encode() in stranger_page.content


# ---------------------------------------------------------------- reviews hook (Prompt 22, mock-first)


def _fake_reviews_module():
    """A stand-in for apps.reviews.services implementing the documented
    contract (no review model exists yet — the hook needs no DB)."""
    import types

    module = types.ModuleType("apps.reviews.services")
    calls = []

    def reviews_for_service(*, service_id: int, viewer=None):
        calls.append((service_id, viewer))
        return module._return

    module._return = []
    module.reviews_for_service = reviews_for_service
    return module, calls


@pytest.mark.django_db
def test_reviews_hook_current_state_stays_honest(service_by_provider):
    """The hook answers None (never a crash) for whatever the REAL landed
    apps.reviews module exposes TODAY — its public surface is
    create/sweep/reputation with NO per-service display query yet, so the
    import fails and the page honestly says 'Reviews coming soon'. WHY
    both branches: the sibling may land the display API mid-suite; then
    the hook must return None or a list, still never crash."""
    module = sys.modules.get("apps.reviews.services")
    if module is not None and getattr(module, "reviews_for_service", None):
        found = services.reviews_for(service_by_provider)
        assert found is None or isinstance(found, list)
    else:
        assert services.reviews_for(service_by_provider) is None


@pytest.mark.django_db
def test_reviews_hook_import_blocked_degrades_to_none(monkeypatch, service_by_provider):
    """Simulated mid-suite landing + removal: the module is blocked the way
    'del + import failure' behaves in import machinery (sys.modules[name] =
    None) — the service page keeps rendering, hook says 'coming soon'.
    WHY simulating: Prompt 22 may land AFTER this wave's suite ran, and
    later removals (a fixup wave) must degrade the same way."""
    sys.modules.pop("apps.reviews.services", None)
    monkeypatch.setitem(sys.modules, "apps.reviews.services", None)
    assert services.reviews_for(service_by_provider) is None


@pytest.mark.django_db
def test_reviews_hook_mock_empty_is_coming_soon(monkeypatch, service_by_provider):
    """Contract honoured: the call reaches reviews with service_id kwarg;
    an EMPTY result means 'no reviews yet' → None → honest coming-soon."""
    module, calls = _fake_reviews_module()
    module._return = []
    monkeypatch.setitem(sys.modules, "apps.reviews.services", module)
    assert services.reviews_for(service_by_provider) is None
    assert calls == [(service_by_provider.pk, None)]   # viewer=None default


@pytest.mark.django_db
def test_reviews_hook_mock_renders_on_detail(monkeypatch, client_auth, service_by_provider):
    """Mock-landed interface: reviews render on the service page and the
    'coming soon' state disappears — the page needs NO change when the
    real apps.reviews ships."""
    module, calls = _fake_reviews_module()
    module._return = [{"rating": 5, "by": "juma", "comment": "Good work."}]
    monkeypatch.setitem(sys.modules, "apps.reviews.services", module)
    body = client_auth.get(f"/services/{service_by_provider.pk}/").content.decode()
    assert "Good work." in body
    assert "Reviews coming soon" not in body
    # The view passed the authenticated viewer through (profiles/mutes of
    # reviews decide what to do with it — the hook only forwards).
    assert calls == [(service_by_provider.pk, _viewer(client_auth))]


@pytest.mark.django_db
def test_reviews_hook_broken_api_degrades_to_none(monkeypatch, service_by_provider):
    """Mid-flight sibling API change (signature/schema churn) must never
    break a service page read — LookupError/TypeError degrade to None."""
    module, _calls = _fake_reviews_module()
    module._return = []

    def boom(*, service_id, viewer=None):
        raise LookupError("reviews schema shifting mid-wave")

    module.reviews_for_service = boom
    monkeypatch.setitem(sys.modules, "apps.reviews.services", module)
    assert services.reviews_for(service_by_provider) is None


def _viewer(client):
    """The logged-in member of the test client (for hook-call assertions)."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.get(username="wanjiku")


# ---------------------------------------------------------------- reviews DOOR (Prompt 22 calls US)


@pytest.mark.django_db
def test_reviews_door_review_for_party_rules(fresh_user, service_by_provider):
    """THE door reviews imports: a member who can READ the service reviews
    its PROVIDER (not party-guessing elsewhere); the provider never
    reviews their own service (door says None — reviews' self-check does
    not re-run on the generic door path)."""
    client_member = get_user_model().objects.create_user("nini", password="pw-123456")
    # Party rule: a readable service's provider is the reviewee.
    assert services.review_for(service_by_provider.pk, client_member.pk) == fresh_user.pk
    # Self-review: provider is refused at the door (None = not a review party).
    assert services.review_for(service_by_provider.pk, fresh_user.pk) is None
    # Unknown reviewer / unknown deal: honest absence, never a crash.
    assert services.review_for(service_by_provider.pk, 999_999) is None
    assert services.review_for(999_999, client_member.pk) is None


@pytest.mark.django_db
def test_reviews_door_review_for_private_states_absent(fresh_user):
    """DRAFT / REMOVED services are not reviewable deals — the door answers
    None; a hidden or removed job cannot collect public stars."""
    from django.contrib.auth import get_user_model

    member = get_user_model().objects.create_user("nini2", password="pw-123456")
    removed = services.create_service(
        provider=fresh_user, title="Picked up", description="d",
        category="catering", ward="Pangani", rate_type=RateType.NEGOTIABLE,
    )
    services.remove_service(removed, by_user=fresh_user)
    draft = ServiceListing.objects.create(
        provider=fresh_user, title="Hidden plan", description="d",
        category="catering", ward="Pangani", rate_type=RateType.NEGOTIABLE,
        status=ContentStatus.DRAFT.value,
    )
    assert services.review_for(removed.pk, member.pk) is None
    assert services.review_for(draft.pk, member.pk) is None


@pytest.mark.django_db
def test_reviews_door_deal_label_honest(fresh_user, service_by_provider):
    """deal_label: the readable service's title, or None (reviews falls
    back to 'Deal #<id>' — the label door NEVER fabricates)."""
    assert services.deal_label(service_by_provider.pk) == service_by_provider.title
    assert services.deal_label(999_999) is None
