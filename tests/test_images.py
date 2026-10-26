"""Prompt 06 acceptance: privacy-clean, budget-bound, one door in.

Fixtures are generated with a SEEDED RNG — deterministic, no binary blobs
in the repo, and the noise image is the adversarial case for the budget
loop (real photos compress far easier).
"""
import io
import random
import uuid

import pytest
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core import images
from apps.media.services import ALLOWED_PURPOSES, save_upload


def _noise_jpeg(width=3000, height=2000, seed=42) -> bytes:
    rng = random.Random(seed)
    rgb = rng.randbytes(width * height * 3)
    img = Image.frombytes("RGB", (width, height), rgb)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _exif_jpeg(width=400, height=200, orientation=6) -> bytes:
    """A JPEG carrying EXIF (Make tag + Orientation) — the privacy hazard."""
    ex = Image.Exif()
    ex[271] = "MtaaniCam"      # Make
    ex[274] = orientation      # Orientation (6 = rotate 90)
    img = Image.new("RGB", (width, height), (200, 30, 90))
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex.tobytes())
    return buf.getvalue()


# --- metadata stripping (KDPA) ----------------------------------------------

@pytest.mark.django_db
def test_exif_stripped_from_output(fresh_user):
    src = _exif_jpeg()
    assert Image.open(io.BytesIO(src)).getexif()[271] == "MtaaniCam"  # hazard exists
    upload = SimpleUploadedFile("cam.jpg", src, content_type="image/jpeg")
    ref = save_upload(file_obj=upload, owner=fresh_user, purpose="post")
    for path_getter in (ref.standard_file, ref.thumb_file):
        out = Image.open(path_getter())
        ex = out.getexif()
        assert not dict(ex), "EXIF survived the pipeline (KDPA leak)"
        assert not dict(ex.get_ifd(0x8825)), "GPS IFD survived (tracking leak)"
        assert "exif" not in out.info
        # NOTE: Pillow's WebP reader adds codec defaults (loop/background) to
        # .info on read — those are container plumbing, not user metadata.


@pytest.mark.django_db
def test_orientation_applied_before_strip(fresh_user):
    """Orientation 6 rotates 90deg: 400x200 becomes 200x400 — and no EXIF."""
    upload = SimpleUploadedFile("rot.jpg", _exif_jpeg(orientation=6), content_type="image/jpeg")
    ref = save_upload(file_obj=upload, owner=fresh_user, purpose="post")
    assert ref.standard_h > ref.standard_w  # transposed, not squashed
    out = Image.open(ref.standard_file())
    assert not dict(out.getexif())


# --- budgets ----------------------------------------------------------------

@pytest.mark.django_db
def test_budgets_enforced_on_adversarial_noise(fresh_user):
    upload = SimpleUploadedFile("noise.jpg", _noise_jpeg(), content_type="image/jpeg")
    ref = save_upload(file_obj=upload, owner=fresh_user, purpose="post")
    assert ref.standard_bytes <= images.STANDARD_MAX_BYTES
    assert max(ref.standard_w, ref.standard_h) <= images.STANDARD_MAX_DIM
    assert ref.thumb_bytes <= images.THUMB_MAX_BYTES
    assert max(ref.thumb_w, ref.thumb_h) <= images.THUMB_MAX_DIM
    # and the original never claims to be one of the variants
    assert ref.original_bytes > ref.standard_bytes


@pytest.mark.django_db
def test_small_photo_keeps_full_dimensions(fresh_user):
    """Images already inside the box are not upscaled or crushed."""
    src = _exif_jpeg(width=300, height=150, orientation=1)
    upload = SimpleUploadedFile("small.jpg", src, content_type="image/jpeg")
    ref = save_upload(file_obj=upload, owner=fresh_user, purpose="guide")
    assert ref.standard_w == 300 and ref.standard_h == 150


# --- rejection paths --------------------------------------------------------

def test_corrupt_upload_rejected():
    with pytest.raises(images.InvalidImage):
        images.process_image(b"definitely not an image at all" * 100)


def test_oversize_upload_rejected():
    with pytest.raises(images.TooLarge):
        images.process_image(b"\x00" * (images.MAX_UPLOAD_BYTES + 1))


@pytest.mark.django_db
def test_unknown_purpose_rejected(fresh_user):
    upload = SimpleUploadedFile("x.jpg", _exif_jpeg(), content_type="image/jpeg")
    with pytest.raises(ValueError):
        save_upload(file_obj=upload, owner=fresh_user, purpose="wallet-drain")


# --- ownership + naming ----------------------------------------------------

@pytest.mark.django_db
def test_refs_are_per_owner(fresh_user, db):
    from django.contrib.auth import get_user_model

    other = get_user_model().objects.create_user("mtoto", password="pass-12345")
    a = save_upload(file_obj=io.BytesIO(_exif_jpeg()), owner=fresh_user, purpose="post")
    b = save_upload(file_obj=io.BytesIO(_exif_jpeg()), owner=other, purpose="post")
    assert a.owner_id == fresh_user.id and b.owner_id == other.id
    assert a.id != b.id


@pytest.mark.django_db
def test_storage_paths_are_uuid_only(fresh_user):
    """Client filenames never touch the filesystem path (traversal guard)."""
    evil = SimpleUploadedFile("../../../../etc/passwd.jpg", _exif_jpeg(), content_type="image/jpeg")
    ref = save_upload(file_obj=evil, owner=fresh_user, purpose="post")
    assert "passwd" not in ref.standard_path
    assert ref.standard_path.startswith("images/post/")
    uuid.UUID(ref.standard_path.split("/")[-2])  # the segment IS a uuid


# --- endpoint ---------------------------------------------------------------

@pytest.mark.django_db
def test_upload_view_requires_auth(client):
    response = client.post("/media/upload/", {})
    assert response.status_code == 403


@pytest.mark.django_db
def test_upload_view_round_trip(client_auth):
    response = client_auth.post(
        "/media/upload/",
        {"purpose": "post", "image": SimpleUploadedFile("p.jpg", _exif_jpeg(), content_type="image/jpeg")},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["purpose"] == "post"
    uuid.UUID(payload["uuid"])
    assert payload["standard"]["bytes"] <= images.STANDARD_MAX_BYTES


@pytest.mark.django_db
def test_upload_view_rejects_garbage(client_auth):
    response = client_auth.post(
        "/media/upload/",
        {"purpose": "post", "image": SimpleUploadedFile("g.jpg", b"garbage", content_type="image/jpeg")},
    )
    assert response.status_code == 400
    assert "picha" in response.json()["error"]  # bilingual message present
