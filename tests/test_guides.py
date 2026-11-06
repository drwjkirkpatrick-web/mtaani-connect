"""Prompt 08 acceptance: guides are creatable, category-searchable, and
revision-safe; risky guides always carry a safety warning; confirmations are
member-only; pages stay inside the 50 KB mobile budget.

The clean() tests are the REAL gate: safety enforcement lives on the models,
not just the forms (PROMPTS.md pitfall), so any future write path (admin,
API, script) inherits it automatically.
"""
import io

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus, SafetyCategory
from apps.guides import services
from apps.guides.models import (
    GUIDE_CATEGORIES,
    Guide,
    GuideConfirmation,
    GuideRevision,
)

# Shape shared by most tests: a plain beginner cooking guide, two steps.
VALID_GUIDE = {
    "title_en": "Cook ugali",
    "title_sw": "",
    "category": "cooking",
    "skill_level": "beginner",
    "safety_category": SafetyCategory.NONE.value,
    "safety_warning": "",
}
VALID_STEPS = [
    {"position": 1, "text": "Boil two cups of water.", "image": None},
    {"position": 2, "text": "Stir in maize flour until firm.", "image": None},
]


@pytest.fixture
def ugali_guide(fresh_user):
    return services.create_guide(author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)


def _step_formset_data(total=3, **overrides):
    """POST payload for the step builder (prefix 'form', 3 rows by default)."""
    data = {
        "form-TOTAL_FORMS": str(total),
        "form-INITIAL_FORMS": "0",
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "form-0-position": "1",
        "form-0-text": "Boil two cups of water.",
        "form-1-position": "2",
        "form-1-text": "Stir in maize flour until firm.",
        "form-2-position": "3",
        "form-2-text": "",
    }
    for n in range(3, total):
        data[f"form-{n}-position"] = str(n + 1)
        data[f"form-{n}-text"] = ""
    data.update(overrides)
    return data


def _create_payload(**overrides):
    data = {**VALID_GUIDE, **_step_formset_data()}
    data.update(overrides)
    return data


def _draft_only_guide(fresh_user):
    """A guide that has never been published (build via models, not services,
    because services.create_guide publishes v1 by design)."""
    guide = Guide.objects.create(created_by=fresh_user, **VALID_GUIDE)
    GuideRevision.objects.create(
        guide=guide,
        version=1,
        title_en=guide.title_en,
        title_sw="",
        safety_category=SafetyCategory.NONE.value,
        safety_warning="",
        status=ContentStatus.DRAFT.value,
    )
    return guide


# ---------------------------------------------------------------- CRUD round-trip


@pytest.mark.django_db
def test_create_round_trip_via_form(client_auth):
    response = client_auth.post("/guides/new/", _create_payload())
    assert response.status_code == 302
    guide = Guide.objects.get()
    assert guide.status == ContentStatus.PUBLISHED.value
    revision = services.latest_published_revision(guide)
    assert revision is not None and revision.version == 1
    assert revision.steps.count() == 2
    # READ: the published page shows the steps of the approved revision
    page = Client().get(f"/guides/{guide.pk}/")
    assert page.status_code == 200
    assert b"Boil two cups of water." in page.content
    assert b"Stir in maize flour until firm." in page.content


@pytest.mark.django_db
def test_edit_creates_draft_revision_and_publish_updates(client_auth, ugali_guide):
    assert client_auth.get(f"/guides/{ugali_guide.pk}/edit/").status_code == 200
    response = client_auth.post(
        f"/guides/{ugali_guide.pk}/edit/",
        _step_formset_data(
            total=4,
            **{"form-0-text": "Boil water in a sufuria.", "form-0-position": "1"},
        )
        | {k: v for k, v in VALID_GUIDE.items()},
    )
    assert response.status_code == 302
    revisions = list(ugali_guide.revisions.order_by("version"))
    assert len(revisions) == 2
    assert revisions[1].status == ContentStatus.DRAFT.value  # every edit = new draft
    # The page the public sees is still the approved v1 (revision safety):
    anon = Client().get(f"/guides/{ugali_guide.pk}/")
    assert b"Boil two cups of water." in anon.content
    assert b"sufuria" not in anon.content
    # Approval: publishing the draft makes it the shown revision
    published = client_auth.post(
        f"/guides/{ugali_guide.pk}/publish/", {"revision_id": revisions[1].pk}
    )
    assert published.status_code == 302
    revisions[1].refresh_from_db()
    assert revisions[1].status == ContentStatus.PUBLISHED.value
    anon = Client().get(f"/guides/{ugali_guide.pk}/")
    assert b"Boil water in a sufuria." in anon.content


@pytest.mark.django_db
def test_delete_hides_guide_but_keeps_rows(client_auth, fresh_user, ugali_guide):
    assert client_auth.get(f"/guides/{ugali_guide.pk}/delete/").status_code == 200
    assert client_auth.post(f"/guides/{ugali_guide.pk}/delete/").status_code == 302
    ugali_guide.refresh_from_db()
    assert ugali_guide.status == ContentStatus.REMOVED.value  # soft delete
    assert GuideRevision.objects.filter(guide=ugali_guide).count() == 1  # rows survive
    assert Client().get(f"/guides/{ugali_guide.pk}/").status_code == 404
    assert b"Cook ugali" not in Client().get("/guides/").content
    other = get_user_model().objects.create_user("ndugu", password="pass-12345")
    client_auth.force_login(other)
    assert client_auth.post(f"/guides/{ugali_guide.pk}/delete/").status_code == 403


@pytest.mark.django_db
def test_author_gates_on_edit_and_publish(client_auth, ugali_guide):
    anon = Client().get(f"/guides/{ugali_guide.pk}/edit/")
    assert anon.status_code == 302  # anonymous -> login
    other = get_user_model().objects.create_user("jirani", password="pass-12345")
    client_auth.force_login(other)
    assert client_auth.get(f"/guides/{ugali_guide.pk}/edit/").status_code == 403
    assert (
        client_auth.post(
            f"/guides/{ugali_guide.pk}/edit/", _create_payload()
        ).status_code
        == 403
    )
    revision = ugali_guide.revisions.get()
    assert (
        client_auth.post(
            f"/guides/{ugali_guide.pk}/publish/", {"revision_id": revision.pk}
        ).status_code
        == 403
    )


# ---------------------------------------------------------------- revision history


@pytest.mark.django_db
def test_history_lists_revisions_with_status(client_auth, fresh_user, ugali_guide):
    services.edit_guide(guide=ugali_guide, editor=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)
    page = client_auth.get(f"/guides/{ugali_guide.pk}/history/")
    assert page.status_code == 200
    html = page.content.decode()
    assert "v1" in html and "v2" in html
    assert "Published" in html and "Draft" in html  # status labels visible
    # history stays public for published guides
    assert Client().get(f"/guides/{ugali_guide.pk}/history/").status_code == 200


@pytest.mark.django_db
def test_under_review_revision_can_still_publish(client_auth, fresh_user, ugali_guide):
    revision = services.edit_guide(
        guide=ugali_guide,
        editor=fresh_user,
        steps=[{"position": 1, "text": "Updated step one.", "image": None}],
        **VALID_GUIDE,
    )
    services.submit_for_review(revision)  # UNDER_REVIEW exists for moderation (15)
    revision.refresh_from_db()
    assert revision.status == ContentStatus.UNDER_REVIEW.value
    services.publish_revision(revision, by_user=fresh_user)  # must not dead-end
    assert services.latest_published_revision(ugali_guide).version == 2


@pytest.mark.django_db
def test_publish_guards(client_auth, fresh_user, ugali_guide):
    v1 = ugali_guide.revisions.get()
    with pytest.raises(ValidationError):  # already published
        services.publish_revision(v1, by_user=fresh_user)
    v1.status = ContentStatus.REMOVED.value
    v1.save()
    with pytest.raises(ValidationError):  # removed snapshots never revive
        services.publish_revision(v1, by_user=fresh_user)
    other = get_user_model().objects.create_user("mtu", password="pass-12345")
    with pytest.raises(PermissionDenied):  # author-only approval
        services.publish_revision(v1, by_user=other)


@pytest.mark.django_db
def test_draft_preview_visible_only_to_author(client_auth, fresh_user):
    guide = _draft_only_guide(fresh_user)
    # the author sees their unpublished work honestly labeled as draft
    page = client_auth.get(f"/guides/{guide.pk}/")
    assert page.status_code == 200
    assert b"Draft preview" in page.content
    # strangers get an honest 404 — not a half-written page
    assert Client().get(f"/guides/{guide.pk}/").status_code == 404


# ---------------------------------------------------------------- safety warnings


@pytest.mark.django_db
def test_safety_warning_enforced_in_model_clean(fresh_user):
    risky = {
        "title_en": "Fix a socket",
        "title_sw": "",
        "category": "repairs",
        "skill_level": "advanced",
        "safety_category": SafetyCategory.MEDICAL.value,
        "safety_warning": "",
        "created_by": fresh_user,
    }
    guide = Guide(**risky)
    with pytest.raises(ValidationError) as err:
        guide.full_clean()  # clean() path, not the form
    assert "safety_warning" in err.value.message_dict
    guide.safety_warning = "Never touch wiring you cannot switch off."
    guide.full_clean()  # now valid
    guide.save()
    revision = GuideRevision(
        guide=guide,
        version=1,
        title_en=guide.title_en,
        safety_category=SafetyCategory.ELECTRICAL.value,  # also risky
        safety_warning="",
        status=ContentStatus.DRAFT.value,
    )
    with pytest.raises(ValidationError) as err:
        revision.full_clean()  # the snapshot carries the same gate
    assert "safety_warning" in err.value.message_dict


@pytest.mark.django_db
def test_create_form_rejects_risky_guide_without_warning(client_auth):
    response = client_auth.post(
        "/guides/new/",
        _create_payload(safety_category=SafetyCategory.MEDICAL.value, safety_warning=""),
    )
    assert response.status_code == 200  # re-rendered with the inline error
    assert b"safety warning is required" in response.content
    assert Guide.objects.count() == 0  # nothing half-saved
    okay = client_auth.post(
        "/guides/new/",
        _create_payload(
            safety_category=SafetyCategory.MEDICAL.value,
            safety_warning="See a clinic, not a chemist, for deep wounds.",
        ),
    )
    assert okay.status_code == 302
    page = Client().get(f"/guides/{Guide.objects.get().pk}/")
    assert b"See a clinic, not a chemist, for deep wounds." in page.content


# ---------------------------------------------------------------- category + search


@pytest.mark.django_db
def test_category_filter(client_auth, fresh_user, ugali_guide):
    services.create_guide(
        author=fresh_user,
        steps=VALID_STEPS,
        **VALID_GUIDE | {"title_en": "Mulch tomatoes", "category": "farming"},
    )
    listing = client_auth.get("/guides/")
    assert b"Cook ugali" in listing.content and b"Mulch tomatoes" in listing.content
    filtered = client_auth.get("/guides/?category=farming")
    assert b"Mulch tomatoes" in filtered.content
    assert b"Cook ugali" not in filtered.content


@pytest.mark.django_db
def test_search_matches_titles_sw_titles_and_step_text(client_auth, fresh_user, ugali_guide):
    services.create_guide(
        author=fresh_user,
        steps=VALID_STEPS,
        **VALID_GUIDE
        | {"title_en": "Mulch tomatoes", "title_sw": "Kupanda nyanya", "category": "farming"},
    )
    html = client_auth.get("/guides/?q=ugali").content  # English title
    # NOTE: the list shows a guide's Kiswahili title first (display_title),
    # so match assertions use whatever title each guide displays.
    assert b"Cook ugali" in html and b"Kupanda nyanya" not in html
    html = client_auth.get("/guides/?q=Kupanda").content  # Kiswahili title
    assert b"Kupanda nyanya" in html and b"Cook ugali" not in html
    html = client_auth.get("/guides/?q=maize").content  # step text (any revision)
    assert b"Cook ugali" in html and b"Kupanda nyanya" in html  # both share steps
    empty = client_auth.get("/guides/?q=zzzqqq")
    assert b"Cook ugali" not in empty.content
    assert b"Coming soon" in empty.content  # honest empty state


# ---------------------------------------------------------------- i18n title fallback


@pytest.mark.django_db
def test_title_fallback_labeled(client_auth, fresh_user, ugali_guide):
    # Kiswahili title missing -> English title stays, visibly labeled
    html = client_auth.get(f"/guides/{ugali_guide.pk}/").content.decode()
    assert "Cook ugali" in html
    assert "no Kiswahili title yet" in html
    # Kiswahili title present -> shown with no fallback label
    other = services.create_guide(
        author=fresh_user,
        steps=VALID_STEPS,
        **VALID_GUIDE | {"title_en": "Graft a mango", "title_sw": "Kupandisha mto"},
    )
    html = client_auth.get(f"/guides/{other.pk}/").content.decode()
    assert "Kupandisha mto" in html
    assert "no Kiswahili title yet" not in html


# ---------------------------------------------------------------- step ordering + builder


@pytest.mark.django_db
def test_steps_render_ordered_by_position(client_auth, fresh_user):
    guide = services.create_guide(
        author=fresh_user,
        steps=[  # deliberately shuffled — storage must order them
            {"position": 3, "text": "STEP-THREE.", "image": None},
            {"position": 1, "text": "STEP-ONE.", "image": None},
            {"position": 2, "text": "STEP-TWO.", "image": None},
        ],
        **VALID_GUIDE,
    )
    html = client_auth.get(f"/guides/{guide.pk}/").content.decode()
    one, two, three = (html.index(f"STEP-{w}") for w in ("ONE", "TWO", "THREE"))
    assert one < two < three


@pytest.mark.django_db
def test_duplicate_positions_rejected(client_auth):
    response = client_auth.post(
        "/guides/new/",
        _create_payload(
            **{"form-0-position": "1", "form-1-position": "1"}
        ),
    )
    assert response.status_code == 200
    assert b"same position" in response.content
    assert Guide.objects.count() == 0


@pytest.mark.django_db
def test_step_text_cap_and_position_floor(client_auth, fresh_user):
    with pytest.raises(ValidationError):  # model cap (MAX_STEP_CHARS)
        services.create_guide(
            author=fresh_user,
            steps=[{"position": 1, "text": "x" * 3000, "image": None}],
            **VALID_GUIDE,
        )
    with pytest.raises(ValidationError):  # position floor on the model
        services.create_guide(
            author=fresh_user,
            steps=[{"position": 0, "text": "Step text.", "image": None}],
            **VALID_GUIDE,
        )


@pytest.mark.django_db
def test_step_photo_flows_through_media_pipeline(client_auth):
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (10, 120, 60)).save(buf, "PNG")
    upload = SimpleUploadedFile("step1.png", buf.getvalue(), content_type="image/png")
    response = client_auth.post(
        "/guides/new/",
        _create_payload(**{"form-0-image_upload": upload}),
    )
    assert response.status_code == 302
    step = Guide.objects.get().revisions.get().steps.first()
    assert step.image is not None
    assert step.image.purpose == "guide"  # Prompt 06 ref, EXIF-stripped WebP
    assert step.image.standard_path.startswith("images/guide/")


# ---------------------------------------------------------------- confirmations


@pytest.mark.django_db
def test_confirmation_requires_login(client, ugali_guide):
    response = client.post(f"/guides/{ugali_guide.pk}/confirm/", {"ward": "Kawangware"})
    assert response.status_code == 302
    assert "/accounts/login" in response.url
    assert GuideConfirmation.objects.count() == 0


@pytest.mark.django_db
def test_confirmation_records_ward_once_per_user(client_auth, ugali_guide):
    assert client_auth.post(
        f"/guides/{ugali_guide.pk}/confirm/",
        {"ward": "Kawangware", "note": "used white maize"},
    ).status_code == 302
    assert GuideConfirmation.objects.count() == 1
    page = client_auth.get(f"/guides/{ugali_guide.pk}/")
    assert b"Kawangware" in page.content and b"used white maize" in page.content
    # confirming again updates the row instead of stacking duplicates
    assert (
        client_auth.post(f"/guides/{ugali_guide.pk}/confirm/", {"ward": "Ruaraka"}).status_code
        == 302
    )
    assert GuideConfirmation.objects.count() == 1
    page = client_auth.get(f"/guides/{ugali_guide.pk}/")
    assert b"Ruaraka" in page.content and b"Kawangware" not in page.content


# ---------------------------------------------------------------- taxonomy + budgets


def test_seed_categories_and_values():
    values = [value for value, _label in GUIDE_CATEGORIES]
    assert values == [
        "cooking", "farming", "repairs", "business",
        "health-education", "crafts", "transport",
    ]
    assert len(values) == len(set(values))  # extendable without collisions
    # enum contract: statuses used by this app store .value strings (never labels)
    assert set(ContentStatus.values) == {
        "DRAFT", "PUBLISHED", "UNDER_REVIEW", "REMOVED",
    }


@pytest.mark.django_db
def test_pages_under_budget(client_auth, fresh_user, ugali_guide):
    anon = Client()
    assert_under_budget(anon.get("/guides/").content.decode("utf-8"), kb=50)
    guide_page = client_auth.get(f"/guides/{ugali_guide.pk}/")  # form + confirmations
    assert guide_page.status_code == 200
    assert_under_budget(guide_page.content.decode("utf-8"), kb=50)
    assert_under_budget(client_auth.get(f"/guides/{ugali_guide.pk}/history/").content.decode("utf-8"), kb=50)


@pytest.mark.django_db
def test_list_pagination_caps_at_20(client_auth, fresh_user):
    for i in range(21):
        services.create_guide(
            author=fresh_user,
            steps=VALID_STEPS[:1],
            **VALID_GUIDE | {"title_en": f"Guide {i:02d}"},
        )
    first = client_auth.get("/guides/")
    assert first.context["page_obj"].paginator.count == 21
    assert len(first.context["guides"]) == 20  # hard list-surface cap
    second = client_auth.get("/guides/?page=2")
    assert len(second.context["guides"]) == 1
