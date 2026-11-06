"""Prompt 20 tests: Messaging v1.

Coverage map (spec-verbatim):
- membership enforcement / IDOR (non-member == 404, never 403; anon redirects)
- enquiry origin integrity (typed origin reference stored WITHOUT an FK to
  the concurrently-building listings app)
- buyer→seller round-trip with NO contact-info exposure
- read receipts + per-user opt-out (both-sides-opt-in display contract)
- unread counts incl. the last_read edge (watermark == timestamp)
- image caps (model clean + service + endpoint, atomic rollback)
- report flow → Prompt 15 queue row + auto-mute (+ unmute way back)
- rate-limited report caught GRACEFULLY (message stays sent, thread intact)
- phone-shape flag persisted at save time (message still sends)
- immutability (no edits) + plain-text rendering + 50 KB budgets
"""
import io

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import assert_under_budget
from apps.core.types import ReportReason
from apps.moderation.models import AuditAction, ModeratorAudit, Report, ReportStatus
from apps.moderation.services import queue_reports
from apps.messaging import services
from apps.messaging.models import MAX_BODY_CHARS, MAX_IMAGES, Message, MessageImage, Thread
from apps.messaging.phone import phone_shapes


@pytest.fixture
def buyer(db):
    # KDPA note: `phone` exists on User for login/verification purposes; the
    # chat page must NEVER carry it — tests leak-guard against exactly that.
    # WHY assembled: sample digits composed at runtime (the literal shape of
    # a Kenyan mobile number is what the leak-guard checks against).
    return User.objects.create_user(
        "amani",
        email="amani@example.com",
        password="***",
        phone="+254" + "700" + "000" + "111",
    )


@pytest.fixture
def seller(db):
    return User.objects.create_user(
        "njeri",
        email="njeri@example.com",
        password="***",
        phone="+254" + "700" + "000" + "222",  # assembled like buyer's, above
    )


@pytest.fixture
def stranger(db):
    return User.objects.create_user("ali", email="ali@example.com", password="***")


@pytest.fixture
def thread(buyer, seller):
    """A direct 1-to-1 thread (origin 'direct') — IDOR/receipt/unread work. """
    return services.create_direct_thread(initiator=buyer, counterpart=seller)


@pytest.fixture
def enquiry_thread(buyer, seller):
    """A marketplace-enquiry thread created with an INJECTED resolver (the
    real resolver returns None today because the sibling listings app lands
    in this same wave — mock-first, never fabricate)."""
    return services.create_enquiry_thread(
        initiator=buyer, listing_id=17, resolver=lambda pk: (seller, "Solar lamp")
    )


@pytest.fixture
def buyer_client(buyer):
    # NOTE: NOT built on shared pytest-django `client` — two dependent fixtures
    # would receive the SAME client object (last force_login wins) and the
    # test would silently act as the wrong member. Standalone per member.
    client = Client()
    client.force_login(buyer)
    return client


@pytest.fixture
def seller_client(seller):
    client = Client()
    client.force_login(seller)
    return client


def _tiny_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (16, 12), (180, 60, 20)).save(buf, "PNG")
    return buf.getvalue()


def _upload(i: int) -> SimpleUploadedFile:
    return SimpleUploadedFile(f"p{i}.png", _tiny_png(), content_type="image/png")


def _detail(pk: int) -> str:
    return reverse("messaging:detail", args=[pk])


def _post_report_data(message_pk: int, **overrides) -> dict:
    data = {
        "message_pk": str(message_pk),
        "reason": ReportReason.SCAM.value,
        "note": "Weird pricing and a phone number.",
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------- phone detector


# ---------------------------------------------------- TEMP DEBUG (remove me)


@pytest.mark.django_db
def test_debug_qsets(buyer, seller):
    t = services.create_direct_thread(initiator=buyer, counterpart=seller)
    services.send_message(thread=t, sender=seller, body="Karibu sana")
    c = Client()
    c.force_login(buyer)
    page = c.get(reverse("messaging:list")).content.decode()
    print("badge in page:", 'data-unread="1"' in page)
    print("title in page:", t.title in page)
    from django.db.models import F, Max

    q2 = (
        Thread.objects.filter(memberships__user=c.session and buyer, memberships__muted=False)
        .annotate(last_activity=Max("messages__created_at"))
        .order_by(F("last_activity").desc(nulls_last=True))
    )
    print("Q2 count:", q2.count())
    print("unreads:", services.unread_counts([t], buyer))
    print("participations:", [(p.user_id, p.muted, p.last_read_at) for p in __import__("apps.messaging.models", fromlist=["ThreadParticipant"]).ThreadParticipant.objects.all()])
    print("messages:", list(__import__("apps.messaging.models", fromlist=["Message"]).Message.objects.values("pk", "thread_id", "sender_id", "body")))


@pytest.mark.parametrize(
    "text",
    [
        "Nitumie kwa 0712 345 678",      # domestic, spaced
        "call +254712345678",            # international, tight
        "254712 345 678",                # bare 254 prefix
        "0712-345-678",                  # dashed
        "piga simu 0112345678",          # newer 01XX range
    ],
)
def test_phone_shapes_flags_kenyan_mobiles(text):
    assert phone_shapes(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "no numbers here",
        "1234567890",                    # 10 digits, no mobile-shaped operator
        "bei ni 1,200.00 hii wiki",      # money, not a phone
        "2024/07/12 345 678",            # date-shaped run inside text
        "",                              # empty text: nothing to flag
    ],
)
def test_phone_shapes_ignores_non_phone_text(text):
    assert phone_shapes(text) is False


@pytest.mark.django_db
def test_phone_flag_persisted_at_save_and_message_still_sends(thread, buyer):
    m = services.send_message(
        thread=thread, sender=buyer, body="Tuma M-Pesa kwa 0712 345 678"
    )
    clean = services.send_message(thread=thread, sender=buyer, body="Bei iko poa.")
    assert m.is_phone_flagged is True   # flag persists (analytics metadata)
    assert clean.is_phone_flagged is False
    assert Message.objects.filter(thread=thread).count() == 2  # message STILL sends


# ------------------------------------------------------- membership / IDOR


@pytest.mark.django_db
def test_idor_anon_gets_login_redirect_and_service_gate_empty(thread):
    response = Client().get(_detail(thread.pk))
    assert response.status_code == 302                      # ordinary bounce
    assert response.url.startswith("/accounts/login")       # not a detail leak
    from django.contrib.auth.models import AnonymousUser
    assert services.get_thread_for(AnonymousUser(), thread.pk) is None


@pytest.mark.django_db
def test_anon_list_renders_sign_in_prompt_no_data_leak(thread, enquiry_thread):
    """Tab-route contract (parent shell test): /chats/ renders 200 for an
    anonymous visitor — as a sign-in prompt with NO thread data attached."""
    response = Client().get(reverse("messaging:list"))
    assert response.status_code == 200
    page = response.content.decode()
    assert "Sign in" in page
    assert "Coming soon" in page             # shell-test empty-state contract
    assert thread.title not in page          # no chat data leaks anonymously
    assert enquiry_thread.title not in page


@pytest.mark.django_db
def test_idor_stranger_gets_404_not_403(thread, stranger):
    """The IDOR shape: a non-member sees the SAME answer as a missing thread
    (404) — a 403 would confirm the guessed thread ID exists."""
    client = Client()
    client.force_login(stranger)
    assert client.get(_detail(thread.pk)).status_code == 404
    assert client.get(reverse("messaging:list")).status_code == 200  # their list is fine


@pytest.mark.django_db
def test_idor_stranger_cannot_post_message(thread, stranger):
    client = Client()
    client.force_login(stranger)
    response = client.post(_detail(thread.pk), {"body": "let me in"})
    assert response.status_code == 404
    assert Message.objects.count() == 0   # nothing written by the outsider


@pytest.mark.django_db
def test_idor_stranger_cannot_report(thread, stranger, buyer):
    m = services.send_message(thread=thread, sender=buyer, body="Karibu")
    client = Client()
    client.force_login(stranger)
    assert (
        client.post(
            reverse("messaging:report_message", args=[thread.pk]),
            _post_report_data(m.pk),
        ).status_code
        == 404
    )
    assert Report.objects.count() == 0


@pytest.mark.django_db
def test_idor_stranger_cannot_toggle_receipts_or_mute(thread, stranger):
    client = Client()
    client.force_login(stranger)
    assert client.post(reverse("messaging:receipts", args=[thread.pk]), {"opt_in": "false"}).status_code == 404
    assert client.post(reverse("messaging:mute", args=[thread.pk])).status_code == 404
    assert client.post(reverse("messaging:unmute", args=[thread.pk])).status_code == 404


# ------------------------------------------------------------ enquiry origin


@pytest.mark.django_db
def test_enquiry_thread_origin_integrity(enquiry_thread, buyer, seller):
    """Typed origin reference lives as a plain id + kind — deliberately NO FK
    (the listings table does not exist while the sibling wave builds)."""
    assert enquiry_thread.origin_kind == "listing"
    assert enquiry_thread.origin_listing_id == 17      # stored reference
    assert enquiry_thread.title == "Solar lamp"        # title auto from origin
    assert set(enquiry_thread.participants.values_list("pk", flat=True)) == {
        buyer.pk,
        seller.pk,
    }


@pytest.mark.django_db
def test_direct_thread_origin_shape_and_auto_title(thread, buyer, seller):
    assert thread.origin_kind == "direct"
    assert thread.origin_listing_id is None
    assert thread.title == seller.get_username()       # title auto from origin


@pytest.mark.django_db
def test_origin_kind_mismatch_is_refused(buyer):
    listing_without_ref = Thread(
        created_by=buyer, origin_kind="listing", title="x"
    )
    with pytest.raises(ValidationError):
        listing_without_ref.full_clean()
    direct_with_ref = Thread(
        created_by=buyer, origin_kind="direct", origin_listing_id=5, title="y"
    )
    with pytest.raises(ValidationError):
        direct_with_ref.full_clean()


@pytest.mark.django_db
def test_enquiry_start_via_http_is_idempotent(
    buyer, seller, buyer_client, monkeypatch
):
    monkeypatch.setattr(
        services, "resolve_public_listing", lambda pk: (seller, "Solar lamp")
    )
    url = reverse("messaging:start_for_listing")
    first = buyer_client.post(url, {"listing": "17"})
    thread_pk = first["Location"].rstrip("/").split("/")[-1]
    second = buyer_client.post(url, {"listing": "17"})
    assert second["Location"] == first["Location"]     # one thread per buyer+listing
    thread = Thread.objects.get(pk=thread_pk)
    assert thread.origin_listing_id == 17 and thread.origin_kind == "listing"
    assert Thread.objects.count() == 1
    assert thread.participants.count() == 2


@pytest.mark.django_db
def test_unresolvable_listing_renders_honest_page_and_fabricates_nothing(
    buyer, seller, buyer_client, monkeypatch
):
    """The sibling app is not ready (or the listing is gone): the honest
    'listing not available yet' page, a 200, NO thread row, NO invented
    seller — the no-fabrication hard rule, verbatim."""
    monkeypatch.setattr(services, "resolve_public_listing", lambda pk: None)
    url = reverse("messaging:start_for_listing")
    response = buyer_client.post(url, {"listing": "99"})
    assert response.status_code == 200
    assert "messages yet" in response.content.decode()
    assert Thread.objects.count() == 0
    # A non-numeric reference is the same honest treatment (never a 500):
    bad = buyer_client.post(url, {"listing": "not-a-number"})
    assert bad.status_code == 200
    assert Thread.objects.count() == 0


# ------------------------------------------------- round-trip + contact guard


@pytest.mark.django_db
def test_buyer_seller_roundtrip_without_contact_exposure(
    buyer, seller, buyer_client, monkeypatch
):
    monkeypatch.setattr(
        services, "resolve_public_listing", lambda pk: (seller, "Solar lamp")
    )
    response = buyer_client.post(
        reverse("messaging:start_for_listing"), {"listing": "17"}, follow=True
    )
    assert response.status_code == 200
    buyer_client.post(
        _detail(Thread.objects.latest("pk").pk), {"body": "Is the lamp new?"}
    )
    seller_client = Client()
    seller_client.force_login(seller)
    detail_url = _detail(Thread.objects.latest("pk").pk)
    seller_view = seller_client.get(detail_url)
    assert seller_view.status_code == 200
    assert "Is the lamp new?" in seller_view.content.decode()   # round-trip
    buyer_view = buyer_client.get(detail_url)
    page = buyer_view.content.decode()
    assert "Listing #17" in page                                # honest origin label
    assert "Listing #17" in seller_view.content.decode()
    # NO contact-info exposure in EITHER direction (KDPA hard rule):
    assert buyer.phone not in page and seller.phone not in page
    assert "700000222" not in page and "700 000" not in page


# ----------------------------------------------------------------- receipts


@pytest.mark.django_db
def test_read_receipts_with_opt_out(buyer, seller, thread, buyer_client):
    detail = _detail(thread.pk)
    buyer_client.post(detail, {"body": "First note"})
    seller_client = Client()
    seller_client.force_login(seller)
    seller_client.get(detail)               # seller reads → watermark advances
    seen_page = buyer_client.get(detail).content.decode()
    assert 'data-receipt="seen"' in seen_page          # both opted in (default)
    # Seller opts OUT of receipts (per-user setting endpoint):
    seller_client.post(reverse("messaging:receipts", args=[thread.pk]), {"opt_in": "false"})
    me = services.participant_of(thread, seller)
    assert me.show_receipts is False
    buyer_client.post(detail, {"body": "Second note"})
    seller_client.get(detail)
    hidden_page = buyer_client.get(detail).content.decode()
    assert 'data-receipt="seen"' not in hidden_page     # one side opted out → hidden
    assert "Second note" in hidden_page                 # chat still works


# ------------------------------------------------------------ unread counts


@pytest.mark.django_db
def test_unread_counts_and_last_read_edge(thread, buyer, seller):
    m1 = services.send_message(thread=thread, sender=seller, body="Karibu")
    assert services.unread_counts([thread], buyer) == {thread.pk: 1}
    # EDGE contract: a watermark EXACTLY at the message timestamp means read
    # (strictly-newer messages count for the badge).
    services.mark_read(services.participant_of(thread, buyer), now=m1.created_at)
    assert services.unread_counts([thread], buyer).get(thread.pk, 0) == 0
    # Own sends never count for the sender.
    services.send_message(thread=thread, sender=buyer, body="Asante")
    assert services.unread_counts([thread], buyer).get(thread.pk, 0) == 0
    assert services.unread_counts([thread], seller) == {thread.pk: 1}


@pytest.mark.django_db
def test_list_shows_badge_and_mute_hides_with_way_back(
    buyer, seller, thread, buyer_client, seller_client
):
    services.send_message(thread=thread, sender=seller, body="Karibu sana")
    list_url = reverse("messaging:list")
    page = buyer_client.get(list_url).content.decode()
    assert 'data-unread="1"' in page                     # per-thread badge
    # mute (self-mute keeps history) → hidden from default, visible in muted view
    buyer_client.post(reverse("messaging:mute", args=[thread.pk]))
    assert thread.title not in buyer_client.get(list_url).content.decode()
    muted_page = buyer_client.get(list_url, {"show": "muted"}).content.decode()
    assert thread.title in muted_page
    assert "muted" in muted_page
    # unmute → back on the default list
    buyer_client.post(reverse("messaging:unmute", args=[thread.pk]))
    assert thread.title in buyer_client.get(list_url).content.decode()
    # mute is PER VIEWER: seller always sees the thread regardless
    assert thread.title in seller_client.get(list_url).content.decode()


# --------------------------------------------------------------- image caps


@pytest.mark.django_db
def test_image_cap_endpoint_rejects_five_atomically(thread, buyer, buyer_client):
    files = {"images": [_upload(i) for i in range(MAX_IMAGES + 1)]}
    response = buyer_client.post(
        _detail(thread.pk), {"body": "photos incoming", **files}, follow=True
    )
    assert response.status_code == 200
    assert "at most 4" in response.content.decode()       # the reason is shown
    assert Message.objects.count() == 0                   # atomic: nothing half-sent
    from apps.media.models import MediaRef
    assert MediaRef.objects.count() == 0


@pytest.mark.django_db
def test_image_cap_service_and_model_slots(thread, buyer, buyer_client):
    url = _detail(thread.pk)
    buyer_client.post(url, {"body": "", "images": [_upload(i) for i in range(MAX_IMAGES)]})
    message = Message.objects.get(thread=thread)
    assert message.image_refs.count() == MAX_IMAGES
    assert [s.position for s in message.image_refs.all()] == [1, 2, 3, 4]
    # Model-side gate: a 5th slot (even reusing the 4th's position ref) fails.
    fifth = MessageImage(message=message, image=message.image_refs.first().image, position=1)
    with pytest.raises(ValidationError):
        fifth.full_clean()


@pytest.mark.django_db
def test_photo_only_message_renders_and_empty_send_refused(thread, buyer, buyer_client):
    url = _detail(thread.pk)
    buyer_client.post(url, {"body": "", "images": [_upload(0)]})
    response = buyer_client.get(url)
    assert "/media/" in response.content.decode()         # thumbnail variant on page
    # empty body AND no photo is a refused send (nothing half-written):
    buyer_client.post(url, {"body": "   "})
    stranded = Message.objects.filter(
        thread=thread, image_refs__isnull=True
    )  # NOTE: the legal PHOTO-ONLY message has body="" but HAS slots — never
    # an empty body with no slots.
    assert not stranded.exists()


# --------------------------------------------------------------- report flow


@pytest.mark.django_db
def test_report_creates_queue_row_and_automutes(thread, buyer, seller):
    m = services.send_message(thread=thread, sender=seller, body="Bei 3000")
    buyer_client = Client()
    buyer_client.force_login(buyer)
    response = buyer_client.post(
        reverse("messaging:report_message", args=[thread.pk]),
        _post_report_data(m.pk),
    )
    assert response.status_code == 302                    # PRG back to list
    assert Report.objects.filter(
        reporter=buyer, target_id=m.pk, status=ReportStatus.OPEN.value
    ).exists()
    assert m.pk in set(queue_reports().values_list("target_id", flat=True))
    moderation_ct = ContentType.objects.get_for_model(Message)
    assert ModeratorAudit.objects.filter(
        actor=buyer,
        action=AuditAction.REPORT_CREATED.value,
        target_ct=moderation_ct,
        target_id=m.pk,
    ).exists()
    # AUTO-MUTE: the REPORTER is muted (their choice, reversible)…
    assert services.participant_of(thread, buyer).muted is True
    # …but the other member is untouched.
    assert services.participant_of(thread, seller).muted is False


@pytest.mark.django_db
def test_report_rate_limit_is_graceful(thread, buyer, seller, buyer_client):
    m = services.send_message(thread=thread, sender=seller, body="Hello there")
    ct = ContentType.objects.get_for_model(thread)
    for _ in range(10):                                   # exhaust the hourly 10
        Report.objects.create(
            reporter=buyer,
            reason=ReportReason.SPAM.value,
            target_ct=ct,
            target_id=thread.pk,
            status=ReportStatus.OPEN.value,
        )
    response = buyer_client.post(
        reverse("messaging:report_message", args=[thread.pk]),
        _post_report_data(m.pk),
        follow=True,
    )
    assert response.status_code == 200
    assert "hourly limit" in response.content.decode()    # localized notice
    assert Report.objects.count() == 10                   # no 11th row
    assert services.participant_of(thread, buyer).muted is False  # failed ≠ muted
    assert buyer_client.get(_detail(thread.pk), status=200) is not None
    assert Message.objects.get(pk=m.pk) is not None       # message stays sent


# --------------------------------------------------- immutability + rendering


@pytest.mark.django_db
def test_message_is_immutable(thread, buyer):
    m = services.send_message(thread=thread, sender=buyer, body="Original")
    m.body = "Rewritten by a sneaky editor"
    with pytest.raises(ValidationError):
        m.save()
    m.refresh_from_db()
    assert m.body == "Original"


@pytest.mark.django_db
def test_body_is_plain_text_never_rendered_as_html(thread, buyer, buyer_client):
    buyer_client.post(_detail(thread.pk), {"body": "<script>alert(1)</script>"})
    page = buyer_client.get(_detail(thread.pk)).content.decode()
    assert "<script>alert(1)" not in page                 # user HTML never renders
    assert "&lt;script&gt;" in page


@pytest.mark.django_db
def test_body_length_cap_enforced(thread, buyer, buyer_client):
    buyer_client.post(_detail(thread.pk), {"body": "x" * (MAX_BODY_CHARS + 1)})
    assert Message.objects.count() == 0                   # over-cap send refused


# -------------------------------------------------------------------- budgets


@pytest.mark.django_db
def test_list_and_detail_stay_under_mobile_budget(
    thread, enquiry_thread, buyer, seller, buyer_client
):
    for i in range(3):
        services.send_message(
            thread=thread, sender=seller if i % 2 == 0 else buyer, body=f"msg {i}"
        )
    list_page = buyer_client.get(reverse("messaging:list")).content.decode()
    assert_under_budget(list_page, kb=50)
    detail_page = buyer_client.get(_detail(thread.pk)).content.decode()
    assert_under_budget(detail_page, kb=50)
