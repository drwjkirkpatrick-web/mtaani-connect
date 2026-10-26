"""Prompt 07 acceptance: allow-list serialization + privacy controls.

The strict key-set test IS the privacy guarantee: a serializer that dumps
the model would leak every future private field — here, a new model field
can only reach the public surface by being added to PUBLIC_KEYS.
"""
import io

import pytest
from django.core.exceptions import PermissionDenied
from PIL import Image

from apps.core.types import LocationVisibility
from apps.profiles.services import PUBLIC_KEYS, get_profile, public_data, set_verified_seller


@pytest.fixture
def profile(fresh_user):
    p = get_profile(fresh_user)
    p.display_name = "Wanjiku"
    p.bio = "Tailor in Kawangware."
    p.skills = ["tailoring", "beads"]
    p.interests = ["farming"]
    p.ward = "Kawangware"
    p.save()
    return p


def test_public_data_exact_allow_list_keys(profile):
    data = public_data(profile, viewer_is_member=True)
    assert set(data) == set(PUBLIC_KEYS)  # strict — nothing extra, nothing missing


def test_phone_and_email_never_in_public_data(profile):
    profile.user.phone = "+254712345678"
    profile.user.email = "secret@example.com"
    profile.user.save()
    data = public_data(profile, viewer_is_member=True)
    assert "phone" not in data and "email" not in data
    assert "+254712345678" not in str(data)


def test_precise_consent_defaults_off_and_never_public(profile):
    assert profile.precise_location_consent is False
    data = public_data(profile, viewer_is_member=True)
    assert "precise_location_consent" not in data


def test_location_granularity_ladder(profile):
    assert public_data(profile, viewer_is_member=True)["location"] == "Kawangware"
    profile.location_visibility = LocationVisibility.NEIGHBOURHOOD
    assert public_data(profile, viewer_is_member=True)["location"] == "Nairobi"
    profile.location_visibility = LocationVisibility.CITY
    assert public_data(profile, viewer_is_member=True)["location"] == "Kenya"
    profile.location_visibility = LocationVisibility.WARD
    profile.ward = ""
    assert public_data(profile, viewer_is_member=True)["location"] is None


def test_members_only_field_hidden_from_anonymous(profile):
    # interests defaults to members-only
    assert public_data(profile, viewer_is_member=False)["interests"] is None
    assert public_data(profile, viewer_is_member=True)["interests"] == ["farming"]


def test_private_never_shown(profile):
    profile.field_visibility = {"bio": "private"}
    profile.save()
    assert public_data(profile, viewer_is_member=True)["bio"] is None
    assert public_data(profile, viewer_is_member=False)["bio"] is None


@pytest.mark.django_db
def test_public_page_respects_visibility(client, fresh_user):
    p = get_profile(fresh_user)
    p.bio = "Public bio."
    p.interests = ["sewing"]
    p.save()  # interests = members-only by default
    url = f"/profiles/u/{fresh_user.username}/"
    anon = client.get(url)
    assert anon.status_code == 200
    assert b"Public bio." in anon.content
    assert b"sewing" not in anon.content          # hidden from anonymous
    from django.contrib.auth import get_user_model
    other = get_user_model().objects.create_user("ndugu", password="pass-12345")
    client.force_login(other)
    member = client.get(url)
    assert b"sewing" in member.content           # visible to members


@pytest.mark.django_db
def test_avatar_flows_through_image_pipeline(client_auth, fresh_user):
    buf = io.BytesIO()
    Image.new("RGB", (600, 400), (10, 120, 60)).save(buf, "PNG")
    buf.seek(0)
    buf.name = "me.png"
    from django.core.files.uploadedfile import SimpleUploadedFile
    upload = SimpleUploadedFile("me.png", buf.getvalue(), content_type="image/png")
    response = client_auth.post("/profiles/me/avatar/", {"image": upload})
    assert response.status_code == 302
    profile = get_profile(fresh_user)
    assert profile.avatar is not None
    assert profile.avatar.purpose == "profile"
    data = public_data(profile, viewer_is_member=False)
    assert data["avatar_thumb"].startswith("images/profile/")


@pytest.mark.django_db
def test_verified_seller_human_gate(fresh_user, db):
    profile = get_profile(fresh_user)
    assert profile.is_verified_seller is False  # default off, never mandatory
    with pytest.raises(PermissionDenied):
        set_verified_seller(profile=profile, by_user=fresh_user)  # not staff
    fresh_user.is_staff = True
    fresh_user.save()
    set_verified_seller(profile=profile, by_user=fresh_user)
    profile.refresh_from_db()
    assert profile.is_verified_seller is True


@pytest.mark.django_db
def test_edit_requires_auth(client):
    assert client.get("/profiles/me/").status_code == 302  # -> login


@pytest.mark.django_db
def test_edit_updates_fields(client_auth, fresh_user):
    response = client_auth.post(
        "/profiles/me/",
        {"display_name": "Wanjiku K.", "bio": "Mkaranja.", "skills": "tailoring, beads",
         "interests": "farming", "ward": "Kawangware"},
    )
    assert response.status_code == 302
    profile = get_profile(fresh_user)
    assert profile.display_name == "Wanjiku K."
    assert profile.skills == ["tailoring", "beads"]


@pytest.mark.django_db
def test_privacy_form_updates_visibility(client_auth, fresh_user):
    response = client_auth.post(
        "/profiles/me/privacy/",
        {"location_visibility": "NEIGHBOURHOOD", "bio_visibility": "public",
         "skills_visibility": "members", "interests_visibility": "private",
         "location_group_visibility": "public"},
    )
    assert response.status_code == 302
    profile = get_profile(fresh_user)
    assert profile.location_visibility == LocationVisibility.NEIGHBOURHOOD
    assert profile.field_visibility == {"bio": "public", "skills": "members",
                                         "interests": "private", "location": "public"}
