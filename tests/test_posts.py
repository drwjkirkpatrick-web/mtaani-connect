"""Prompt 10 acceptance: posts are creatable, editable, soft-removable, tag-
filterable; the 4-photo cap is enforced on the MODEL clean() AND the upload
endpoint (client checks are decoration — PROMPTS.md pitfall); typed guide
references render server-side with HONEST fallback states; and a feed-item
page plus the list page both stay under the 50 KB mobile budget even with
3 photo posts seeded through the real media pipeline.

All reference reads cross into apps.guides ONLY through its public service
functions (visible_guides, latest_published_revision) — no guide internals.
"""
import io

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus
from apps.guides.services import create_guide, remove_guide
from apps.media.services import save_upload
from apps.posts import services
from apps.posts.models import MAX_IMAGES, MAX_POST_CHARS, Post, PostImage

PUBLISHED = ContentStatus.PUBLISHED.value
REMOVED = ContentStatus.REMOVED.value


# --- tiny deterministic test photos (no binary blobs in the repo) ------------

def _tiny_jpeg(width=48, height=32) -> bytes:
    img = Image.new("RGB", (width, height), (40, 120, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _make_photo(user):
    """One real MediaRef through the Prompt 06 pipeline — never hand-made rows
    (the budget/pitfall tests must measure the same path production uses)."""
    upload = SimpleUploadedFile("p.jpg", _tiny_jpeg(), content_type="image/jpeg")
    return save_upload(file_obj=upload, owner=user, purpose="post")


@pytest.fixture
def author(fresh_user):
    return fresh_user


@pytest.fixture
def post(author, db):
    return services.create_post(
        author=author, text="Karibu jioni! Nyama choma Karura Gate ni 100 bob.",
        ref_listing=None,
    )


# ---------------------------------------------------------------- CRUD round-trip


@pytest.mark.django_db
def test_create_round_trip_via_form(client_auth):
    """One tap: create → PUBLISHED → visible on list + detail (word-of-mouth:
    publishing must not need a second approval step for members)."""
    response = client_auth.post(
        "/posts/new/", {"text": "Unga bei imeshuka Kwa Mama Njeri.", "educational": ""}
    )
    assert response.status_code == 302
    article = Post.objects.get()
    assert article.status == PUBLISHED
    assert article.author.username == "wanjiku"
    detail = Client().get(f"/posts/{article.pk}/")
    assert detail.status_code == 200
    assert "Unga bei imeshuka".encode() in detail.content
    listing = Client().get("/posts/")
    assert len(listing.context["posts"]) == 1


@pytest.mark.django_db
def test_create_requires_login(client):
    assert client.get("/posts/new/").status_code == 302


@pytest.mark.django_db
def test_blank_text_rejected_at_form(client_auth):
    response = client_auth.post("/posts/new/", {"text": "   "})
    assert response.status_code == 200  # form re-render, never a 500
    assert Post.objects.count() == 0


@pytest.mark.django_db
def test_text_cap_enforced_on_model(author):
    """Over-2,000-chars text fails on the MODEL — the form's maxlength attr
    is decoration (PROMPTS.md pitfall pattern)."""
    with pytest.raises(ValidationError):
        post = Post(author=author, text="x" * (MAX_POST_CHARS + 1))
        post.full_clean()


@pytest.mark.django_db
def test_chars_remaining_helper(author):
    post = services.create_post(author=author, text="Karibu sana")
    assert post.chars_remaining == MAX_POST_CHARS - len("Karibu sana")


@pytest.mark.django_db
def test_edit_round_trip_and_author_gate(client_auth, fresh_user):
    article = services.create_post(author=fresh_user, text="Tano hii.")
    # A DIFFERENT member is refused (PermissionDenied → 403), even logged in.
    other = get_user_model().objects.create_user("juma", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    assert (
        client_other.post(
            f"/posts/{article.pk}/edit/", {"text": "hacked", "educational": ""}
        ).status_code
        == 403
    )
    # The author's edit round-trips and keeps the published status.
    response = client_auth.post(
        f"/posts/{article.pk}/edit/",
        {"text": "Bei mpya: 20 bob.", "educational": "on", "ref_guide": ""},
    )
    assert response.status_code == 302
    article.refresh_from_db()
    assert article.text == "Bei mpya: 20 bob."
    assert article.educational is True
    assert article.status == PUBLISHED  # status is NOT editable through edit


@pytest.mark.django_db
def test_soft_remove_hides_everywhere_but_keeps_rows(client_auth, fresh_user):
    """Soft delete: REMOVED posts vanish from list + other members' detail,
    rows survive (moderation evidence), author still sees a removed badge."""
    article = services.create_post(author=fresh_user, text="Goodbye.")
    assert client_auth.post(f"/posts/{article.pk}/delete/").status_code == 302
    article.refresh_from_db()
    assert article.status == REMOVED
    assert f"/posts/{article.pk}/".encode() not in client_auth.get("/posts/").content
    assert Client().get(f"/posts/{article.pk}/").status_code == 404
    own = client_auth.get(f"/posts/{article.pk}/")
    assert own.status_code == 200 and b"Removed" in own.content


@pytest.mark.django_db
def test_draft_is_private_except_author(client_auth, fresh_user):
    article = Post.objects.create(
        author=fresh_user, text="Draft not yet shared.",
        status=ContentStatus.DRAFT.value,
    )
    assert f"/posts/{article.pk}/".encode() not in Client().get("/posts/").content
    assert Client().get(f"/posts/{article.pk}/").status_code == 404
    own = client_auth.get(f"/posts/{article.pk}/")
    assert own.status_code == 200 and b"Draft" in own.content


# ---------------------------------------------------------------- 4-image cap


@pytest.fixture
def photo_post(author, db):
    return services.create_post(author=author, text="Picha za jioni.")


def _fill_photo_slots(article, user, n=MAX_IMAGES):
    return [services.add_image(article, _make_photo(user)) for _ in range(n)]


@pytest.mark.django_db
def test_post_clean_rejects_more_than_four_photos(author, photo_post):
    """THE model gate: even a FUTURE write path that attaches 5 photos (admin,
    API, script) and then full_clean()s the post fails — Post.clean() counts
    the saved attachment rows (PROMPTS.md pitfall)."""
    for n in range(5):
        PostImage.objects.create(post=photo_post, image=_make_photo(author), position=n + 1)
    with pytest.raises(ValidationError):
        photo_post.full_clean()


@pytest.mark.django_db
def test_service_add_image_enforces_cap(author, photo_post, fresh_user):
    assert MAX_IMAGES == 4
    for _ in range(4):
        services.add_image(photo_post, _make_photo(author))
    fifth = _make_photo(author)
    with pytest.raises(ValidationError):
        services.add_image(photo_post, fifth)


@pytest.mark.django_db
def test_foreign_photo_ref_rejected(author, photo_post):
    """KDPA + correctness: you attach YOUR upload, never someone else's."""
    stranger = get_user_model().objects.create_user("mgeni", password="pw-123456")
    stranger_ref = _make_photo(stranger)
    with pytest.raises(PermissionDenied):
        services.add_image(photo_post, stranger_ref)


@pytest.mark.django_db
def test_position_bounds_and_collisions_rejected(author, photo_post):
    ref = _make_photo(author)
    for bad in (0, 5):
        row = PostImage(post=photo_post, image=ref, position=bad)
        with pytest.raises(ValidationError):
            row.full_clean()
    first = services.add_image(photo_post, ref)
    clash = PostImage(post=photo_post, image=_make_photo(author), position=first.position)
    with pytest.raises(ValidationError):
        clash.full_clean()


@pytest.mark.django_db
def test_duplicate_photo_rejected(author, photo_post):
    ref = _make_photo(author)
    services.add_image(photo_post, ref)
    with pytest.raises(ValidationError):
        services.add_image(photo_post, ref)
    assert photo_post.attachments.count() == 1


# ---------------------------------------------------------------- upload endpoint


@pytest.mark.django_db
def test_upload_endpoint_happy_path_orders_slots(client_auth, fresh_user):
    article = services.create_post(author=fresh_user, text="Two photos today.")
    for n in (1, 2):
        response = client_auth.post(
            f"/posts/{article.pk}/images/upload/",
            {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
        )
        assert response.status_code == 201, response.content
        assert response.json()["position"] == n  # slots fill in order
    attachments = list(article.attachments.order_by("position"))
    assert [r.position for r in attachments] == [1, 2]
    detail = Client().get(f"/posts/{article.pk}/")
    body = detail.content.decode()
    # The detail page renders the REAL pipeline output (standard WebP variant)
    assert "/media/images/post/" in body and all(
        r.image.standard_path.split("/")[-1] == "standard.webp" for r in attachments
    )


@pytest.mark.django_db
def test_upload_endpoint_enforces_four_photo_cap_server_side(author, client_auth, photo_post):
    """THE pitfall test: 4 photos already attached → the endpoint refuses a
    5th even though the CLIENT simply posts another file. Count never moves."""
    _fill_photo_slots(photo_post, author)
    response = client_auth.post(
        f"/posts/{photo_post.pk}/images/upload/",
        {"image": SimpleUploadedFile("fifth.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 400
    assert photo_post.attachments.count() == MAX_IMAGES


@pytest.mark.django_db
def test_upload_endpoint_refuses_other_member(photo_post, fresh_user):
    other = get_user_model().objects.create_user("nani", password="pw-123456")
    client_other = Client()
    client_other.force_login(other)
    before = photo_post.attachments.count()
    response = client_other.post(
        f"/posts/{photo_post.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403
    assert photo_post.attachments.count() == before


@pytest.mark.django_db
def test_upload_endpoint_refuses_anonymous(photo_post):
    response = Client().post(
        f"/posts/{photo_post.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", _tiny_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_upload_endpoint_rejects_corrupt_and_missing(photo_post, client_auth):
    corrupt = client_auth.post(
        f"/posts/{photo_post.pk}/images/upload/",
        {"image": SimpleUploadedFile("f.jpg", b"not an image" * 20, content_type="image/jpeg")},
    )
    assert corrupt.status_code == 400
    missing = client_auth.post(f"/posts/{photo_post.pk}/images/upload/", {})
    assert missing.status_code == 400
    assert photo_post.attachments.count() == 0


# ---------------------------------------------------------------- educational filter


@pytest.mark.django_db
def test_educational_tag_filter(fresh_user):
    services.create_post(author=fresh_user, text="Normal word-of-mouth.")
    tip = services.create_post(
        author=fresh_user, text="Jinsi ya kupika ndengu bila stress.", educational=True
    )
    services.create_post(author=fresh_user, text="Njia bypass pale Githurai 45.")
    only_tagged = Client().get("/posts/?tag=educational")
    page_posts = [item["post"].pk for item in only_tagged.context["posts"]]
    assert page_posts == [tip.pk]
    everything = Client().get("/posts/")
    assert len(everything.context["posts"]) == 3


# ---------------------------------------------------------------- reference previews


@pytest.fixture
def ugali_guide(fresh_user):
    return create_guide(
        author=fresh_user,
        title_en="Cook ugali",
        category="cooking",
        skill_level="beginner",
        steps=[{"position": 1, "text": "Boil water.", "image": None}],
    )


@pytest.mark.django_db
def test_guide_reference_renders_published_title(ugali_guide, fresh_user):
    """Typed ref, not a URL: the preview card renders the guide's title via
    the guides app's PUBLIC functions and links its public page."""
    article = services.create_post(
        author=fresh_user, text="Nilichofuata mwongozo huu.",
        ref_guide=ugali_guide,
    )
    detail = Client().get(f"/posts/{article.pk}/").content.decode()
    assert "Cook ugali" in detail
    assert f'href="/guides/{ugali_guide.pk}/"' in detail


@pytest.mark.django_db
def test_unavailable_guide_reference_is_honest(ugali_guide, fresh_user):
    """A guide link whose target is no longer visible must SAY so — never
    fabricate a plausible title (PROMPTS.md no-fabrication rule)."""
    services.create_post(author=fresh_user, text="Ilianza hivi.", ref_guide=ugali_guide)
    remove_guide(ugali_guide, by_user=fresh_user)
    # A REMOVED guide is a list-surface invisible for EVERYONE (guides policy),
    # so even the author's own post preview degrades to the honesty state.
    article = Post.objects.get()
    detail = Client().get(f"/posts/{article.pk}/").content.decode()
    assert "Guide no longer available" in detail
    assert f'href="/guides/{ugali_guide.pk}/"' not in detail


@pytest.mark.django_db
def test_listing_preview_is_honest_placeholder(fresh_user):
    """Prompt 17 (listings) does not exist yet: the preview is the NO_DATA
    honesty placeholder — NO invented listing data, NO dead link."""
    article = Post.objects.create(
        author=fresh_user, text="Nitauza mahindi hapa.", status=PUBLISHED,
        ref_listing=9,
    )
    detail = Client().get(f"/posts/{article.pk}/").content.decode()
    assert "Listing preview coming soon" in detail
    # The placeholder is a non-link card (no href) — no invented data either.
    assert '<div class="card linkcard">' in detail
    assert '<a class="card linkcard"' not in detail
    # And the list page shows the same placeholder card.
    listing = Client().get("/posts/").content.decode()
    assert "Listing preview coming soon" in listing


# ---------------------------------------------------------------- mobile budgets


@pytest.mark.django_db
def test_budget_with_three_photo_posts(author, fresh_user):
    """Feed-item page AND list page each stay ≤50 KB HTML with 3 photo posts
    of FOUR photos each (adversarial; images are separate cached requests)."""
    for n in range(3):
        article = services.create_post(
            author=fresh_user,
            text=f"Ripoti ya siku #{n}: "
            "bei za mboga ni hizi tu — hakuna novel hapa. Short post culture!",
        )
        _fill_photo_slots(article, fresh_user)
    detail = Client().get(f"/posts/{Post.objects.order_by('pk').first().pk}/")
    listing = Client().get("/posts/")
    assert_under_budget(detail.content.decode(), kb=50)
    assert_under_budget(listing.content.decode(), kb=50)
    assert detail.content.count(b"/media/images/post/") == 4
    assert listing.content.count(b"/media/images/post/") == 3  # one thumb each
