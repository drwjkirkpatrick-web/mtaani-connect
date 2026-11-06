"""Prompt 09 tests: offline saved guides — manifest, hashes, quota, honesty.

The service-worker JS itself is the PARENT's file (static/js/sw.js — see
the verbatim contract in apps/guides/offline.py); these tests pin the
server-side behaviour the worker is implemented to: the
/guides/offline-manifest.json payload, the save/unsave JSON endpoints,
revision-hash determinism, the hash-only stale decision (NEVER dates), the
50 MB quota warning, login/CSRF gates and the offline page's 50 KB budget.

NOTE on the big-MediaRef fixture: a MediaRef row is metadata (paths + byte
counts) — creating one with a large standard_bytes and no file on disk is a
unit fixture for quota arithmetic, not fabricated app content (no file is
ever claimed to exist; offline.estimate_used_bytes only reads DB bytes).
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.core.testing import assert_under_budget
from apps.core.types import ContentStatus, SafetyCategory
from apps.guides import offline
from apps.guides.models import Guide, GuideRevision, OfflineSavedGuide
from apps.guides.services import (
    create_guide,
    edit_guide,
    latest_published_revision,
    publish_revision,
    remove_guide,
)
from apps.media.models import MediaRef

MB = 1024 * 1024

# Small shared shape (same pattern as tests/test_guides.py).
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
    return create_guide(author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)


@pytest.fixture
def other_member(db):
    User = get_user_model()
    return User.objects.create_user(username="juma", email="juma@example.com", password="***")


def _draft_only_guide(user, title="Seed a seedbed"):
    """A guide that has never been published (built via models — services
    publishes v1 by design). The manifest must NOT invent a hash for it."""
    guide = Guide.objects.create(created_by=user, **{**VALID_GUIDE, "title_en": title})
    GuideRevision.objects.create(
        guide=guide,
        version=1,
        title_en=title,
        title_sw="",
        safety_category=SafetyCategory.NONE.value,
        safety_warning="",
        status=ContentStatus.DRAFT.value,
    )
    return guide


def _media_ref(owner, std_bytes):
    """Metadata-only MediaRef for quota arithmetic (no file claimed)."""
    uid = uuid.uuid4()
    return MediaRef.objects.create(
        id=uid,
        owner=owner,
        purpose="guide",
        standard_path=f"images/guide/{uid}/standard.webp",
        standard_bytes=std_bytes,
        standard_w=800,
        standard_h=600,
        thumb_path=f"images/guide/{uid}/thumb.webp",
        thumb_bytes=1024,
        thumb_w=160,
        thumb_h=120,
        original_bytes=std_bytes * 3,
        original_format="WEBP",
    )


def _attach_image(revision, ref, position=1):
    """Point one step of a revision at a media ref (unit-fixture wiring)."""
    step = revision.steps.get(position=position)
    step.image = ref
    step.save()


# ------------------------------------------------------- save/unsave lifecycle


@pytest.mark.django_db
def test_save_then_unsave_round_trip(client_auth, ugali_guide):
    assert OfflineSavedGuide.objects.count() == 0
    save = client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/")
    assert save.status_code == 200
    body = save.json()
    assert body["saved"] is True
    assert body["hash"].startswith("gsh1:")
    assert body["url"] == f"/guides/{ugali_guide.pk}/"
    assert body["pages"] == [f"/guides/{ugali_guide.pk}/"]  # SW cache target
    assert OfflineSavedGuide.objects.count() == 1

    # Idempotent: saving again never duplicates (unique user+guide).
    second = client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/").json()
    assert second["hash"] == body["hash"]
    assert OfflineSavedGuide.objects.count() == 1

    unsave = client_auth.post(f"/guides/{ugali_guide.pk}/offline/unsave/")
    assert unsave.status_code == 200
    assert unsave.json() == {"saved": False, "hash": None, "guide": ugali_guide.pk}
    assert OfflineSavedGuide.objects.count() == 0


@pytest.mark.django_db
def test_save_and_manifest_gates(client, ugali_guide, fresh_user):
    """Anonymous: save is a 302 login redirect (state change, never silent),
    save via GET is refused (405 — POST-only), manifest is a 302 (a member's
    offline list is theirs alone — never anonymous-leaked)."""
    # Anonymous POST -> login redirect
    assert client.post(f"/guides/{ugali_guide.pk}/offline/save/").status_code == 302
    # Logged-in but wrong method -> 405 (require_POST)
    anon = Client()
    anon.force_login(fresh_user)
    assert anon.get(f"/guides/{ugali_guide.pk}/offline/save/").status_code == 405
    # Manifest: the member sees their own (200 / auth works), the anonymous
    # visitor is redirected to login — the list is never anonymous-leaked.
    assert anon.get("/guides/offline-manifest.json").status_code == 200
    assert client.get("/guides/offline-manifest.json").status_code == 302
    login_url = client.get("/guides/offline-manifest.json")["Location"]
    assert login_url.startswith("/accounts/login/")  # + ?next= target


@pytest.mark.django_db
def test_manifest_is_scoped_to_its_users_saves(client_auth, ugali_guide, other_member):
    """Saved guides of ONE member never appear in another member's manifest."""
    create_guide(author=other_member, steps=VALID_STEPS, **{**VALID_GUIDE, "title_en": "Juma's tip"})
    offline.save_offline(user=other_member, guide=_draft_only_guide(other_member, "Juma's seedbed"))
    client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/")

    data = client_auth.get("/guides/offline-manifest.json").json()
    assert [g["guide"] for g in data["guides"]] == [ugali_guide.pk]
    assert data["count"] == 1  # juma's saves stay juma's


# ------------------------------------------------------------ manifest + hashes


@pytest.mark.django_db
def test_manifest_lists_saved_guides_with_correct_hash(client_auth, ugali_guide):
    client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/")
    data = client_auth.get("/guides/offline-manifest.json").json()

    assert data["pages"] == 1 and data["page"] == 1
    entry = data["guides"][0]
    assert entry["guide"] == ugali_guide.pk
    assert entry["url"] == f"/guides/{ugali_guide.pk}/"
    assert entry["state"] == offline.STATE_PUBLISHED == "published"
    assert entry["version"] == 1
    # The manifest hash is the LATEST PUBLISHED revision's content hash.
    assert entry["hash"] == offline.revision_hash(latest_published_revision(ugali_guide))
    assert entry["hash"] == offline.latest_published_hash(ugali_guide)
    # Quota block always present; no images -> zero bytes, warning off.
    assert data["quota"]["limit_bytes"] == offline.QUOTA_WARNING_BYTES == 50 * 1024 * 1024
    assert data["quota"]["used_bytes"] == 0
    assert data["quota"]["warning"] is False


@pytest.mark.django_db
def test_hash_stability_identical_content(fresh_user, other_member):
    """THE Prompt 09 core: identical content must hash identical — across
    different guides, authors, versions and timestamps (content-based, so
    the worker's stored hash only ever changes when the words change)."""
    g1 = create_guide(author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)
    g2 = create_guide(author=other_member, steps=VALID_STEPS, **VALID_GUIDE)

    h1 = offline.revision_hash(g1.revisions.get(version=1))
    h2 = offline.revision_hash(g2.revisions.get(version=1))
    assert h1 == h2 and h1.startswith("gsh1:")

    # Same content saved again as a fresh DRAFT revision -> same hash.
    draft = edit_guide(  # identical meta + steps; creates a new revision
        guide=g1,
        editor=fresh_user,
        steps=[{**s, "image": None} for s in VALID_STEPS],
        **VALID_GUIDE,
    )
    assert draft.status == ContentStatus.DRAFT.value
    assert offline.revision_hash(draft) == h1

    # Deterministic across calls (pure function of content).
    assert offline.revision_hash(draft) == offline.revision_hash(draft)


@pytest.mark.django_db
def test_hash_changes_when_published_content_changes(client_auth, fresh_user, ugali_guide):
    before = offline.latest_published_hash(ugali_guide)
    # An edit is DRAFT by design: the published page (and its hash) stands
    # until the revision is approved — offline caches stay valid meanwhile.
    draft = edit_guide(
        guide=ugali_guide,
        editor=fresh_user,
        steps=[{**s, "image": None} for s in VALID_STEPS]
        + [{"position": 3, "text": "Serve hot with sukuma wiki.", "image": None}],
        **VALID_GUIDE,
    )
    assert offline.latest_published_hash(ugali_guide) == before  # drafts don't count
    publish_revision(draft, by_user=fresh_user)
    after = offline.latest_published_hash(ugali_guide)
    assert after != before


def test_sync_decision_fetch_only_on_hash_change():
    """The stale check unit spec — dates NEVER appear in it; a no_data
    entry (hash null) can never trigger a download (nothing to fetch)."""
    entry = {"hash": "gsh1:" + "a" * 64}
    assert offline.sync_decision(entry, None) == "fetch"          # first cache
    assert offline.sync_decision(entry, "gsh1:" + "a" * 64) == "skip"  # same content
    assert offline.sync_decision(entry, "gsh1:" + "b" * 64) == "fetch"  # changed
    no_data = {"hash": None, "state": offline.STATE_NO_DATA}
    assert offline.sync_decision(no_data, "gsh1:" + "a" * 64) == "skip"
    assert offline.sync_decision(no_data, None) == "skip"


@pytest.mark.django_db
def test_end_to_end_hash_change_triggers_refetch_only_once(client_auth, fresh_user, ugali_guide):
    """Worker walk on the real endpoint: first sync fetches, same content
    skips, a real content change fetches exactly then, and is skipped again
    once cached (a saved hash is compared, not the publish date)."""
    saved = client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/").json()

    def current_entry():
        return client_auth.get("/guides/offline-manifest.json").json()["guides"][0]

    assert offline.sync_decision(current_entry(), None) == "fetch"
    entry = current_entry()
    assert entry["hash"] == saved["hash"]  # nothing published changed since save
    assert offline.sync_decision(entry, entry["hash"]) == "skip"

    draft = edit_guide(
        guide=ugali_guide,
        editor=fresh_user,
        steps=[{**s, "image": None} for s in VALID_STEPS],
        **{**VALID_GUIDE, "title_en": "Cook ugali differently"},
    )
    publish_revision(draft, by_user=fresh_user)
    entry = current_entry()
    assert offline.sync_decision(entry, saved["hash"]) == "fetch"
    # ...after refetching (stored == new hash), the next walk skips again.
    assert offline.sync_decision(entry, entry["hash"]) == "skip"


@pytest.mark.django_db
def test_manifest_no_data_flag_never_fabricates_hash(client_auth, fresh_user):
    guide = _draft_only_guide(fresh_user)
    body = client_auth.post(f"/guides/{guide.pk}/offline/save/").json()
    assert body["saved"] is True
    assert body["hash"] is None  # no published revision -> no invented hash
    data = client_auth.get("/guides/offline-manifest.json").json()
    entry = data["guides"][0]
    assert entry["hash"] is None
    assert entry["version"] is None
    assert entry["state"] == offline.STATE_NO_DATA == "no_data"


@pytest.mark.django_db
def test_removed_guide_shows_no_data(client_auth, fresh_user, ugali_guide):
    offline.save_offline(user=fresh_user, guide=ugali_guide)
    remove_guide(ugali_guide, by_user=fresh_user)
    # A removed guide cannot be NEWLY saved (its page is gone).
    assert client_auth.post(f"/guides/{ugali_guide.pk}/offline/save/").status_code == 404
    # But the stale bookmark stays visible — honestly flagged, not fabricated.
    entry = client_auth.get("/guides/offline-manifest.json").json()["guides"][0]
    assert entry["state"] == offline.STATE_NO_DATA
    assert entry["hash"] is None


@pytest.mark.django_db
def test_unsave_reflected_in_manifest(client_auth, fresh_user):
    g1 = create_guide(author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)
    g2 = create_guide(
        author=fresh_user, steps=VALID_STEPS, **{**VALID_GUIDE, "title_en": "Boil beans"}
    )
    offline.save_offline(user=fresh_user, guide=g1)
    offline.save_offline(user=fresh_user, guide=g2)
    data = client_auth.get("/guides/offline-manifest.json").json()
    assert {g["guide"] for g in data["guides"]} == {g1.pk, g2.pk}
    client_auth.post(f"/guides/{g2.pk}/offline/unsave/")
    data = client_auth.get("/guides/offline-manifest.json").json()
    assert [g["guide"] for g in data["guides"]] == [g1.pk]
    assert data["count"] == 1


@pytest.mark.django_db
def test_hash_changes_when_step_photo_swaps(fresh_user):
    """A step photo is content: swapping the image must change the hash
    (worker re-fetches), while keeping the same photo must not."""
    ref1 = _media_ref(fresh_user, 800)
    ref2 = _media_ref(fresh_user, 900)
    g1 = create_guide(author=fresh_user, steps=VALID_STEPS, **VALID_GUIDE)
    revision = g1.revisions.get(version=1)
    base = offline.revision_hash(revision)
    _attach_image(revision, ref1)
    with_photo = offline.revision_hash(revision)
    assert with_photo != base  # adding a photo changes the content
    _attach_image(revision, ref2)
    assert offline.revision_hash(revision) != with_photo  # swapping does too


# ------------------------------------------------------------------ quota


@pytest.mark.django_db
def test_quota_warning_off_without_images(client_auth, fresh_user, ugali_guide):
    offline.save_offline(user=fresh_user, guide=ugali_guide)
    data = client_auth.get("/guides/offline-manifest.json").json()
    assert data["quota"]["warning"] is False
    assert data["quota"]["used_bytes"] == 0


@pytest.mark.django_db
def test_quota_warning_on_over_50mb_counted_from_latest_published_revision(
    client_auth, fresh_user, ugali_guide
):
    revision = ugali_guide.revisions.get(version=1)
    ref1 = _media_ref(fresh_user, int(26 * MB))
    ref2 = _media_ref(fresh_user, int(26 * MB))
    _attach_image(revision, ref1, position=1)
    _attach_image(revision, ref2, position=2)
    offline.save_offline(user=fresh_user, guide=ugali_guide)

    used = offline.estimate_used_bytes(fresh_user)
    assert used == int(26 * MB) * 2  # only the latest PUBLISHED page's images
    data = client_auth.get("/guides/offline-manifest.json").json()
    assert data["quota"]["warning"] is True
    assert data["quota"]["used_bytes"] == used

    # Entry carries the exact /media/ URLs the detail page renders — the SW
    # precaches them so images survive airplane mode (Prompt 09 acceptance).
    entry = data["guides"][0]
    assert sorted(entry["images"]) == sorted(
        f"/media/{ref.standard_path}" for ref in (ref1, ref2)
    )


# --------------------------------------------------- offline list page (HTML)


@pytest.mark.django_db
def test_offline_page_renders_saved_guides_under_budget(client_auth, fresh_user, ugali_guide):
    draft_guide = _draft_only_guide(fresh_user)
    offline.save_offline(user=fresh_user, guide=ugali_guide)
    offline.save_offline(user=fresh_user, guide=draft_guide)
    response = client_auth.get("/guides/offline/")
    assert response.status_code == 200
    html = response.content.decode()
    assert "Cook ugali" in html
    assert "Seed a seedbed" in html
    # Honesty states visible server-side: ready marker + the no-data label.
    assert "No published version yet" in html
    assert_under_budget(html, kb=50)  # Prompt 09 budget: own page, own test


@pytest.mark.django_db
def test_offline_page_warns_over_quota(client_auth, fresh_user, ugali_guide):
    _attach_image(
        ugali_guide.revisions.get(version=1), _media_ref(fresh_user, int(51 * MB))
    )
    offline.save_offline(user=fresh_user, guide=ugali_guide)
    html = client_auth.get("/guides/offline/").content.decode()
    assert "50 MB" in html  # the quota banner (contract item 7)


@pytest.mark.django_db
def test_offline_page_needs_login(client, ugali_guide):
    assert client.get("/guides/offline/").status_code == 302


# ------------------------------------------- manifest pagination (hard rule)


@pytest.mark.django_db
def test_manifest_paginates_at_20_items(client_auth, fresh_user):
    """API list responses <= 20 items (PROMPTS.md hard rule) — the manifest
    walks ?page=N; the SW spec (offline.py contract item 2) relies on this."""
    for n in range(21):
        guide = create_guide(
            author=fresh_user,
            steps=[{"position": 1, "text": f"Step {n + 1}.", "image": None}],
            **{**VALID_GUIDE, "title_en": f"Guide {n + 1}"},
        )
        offline.save_offline(user=fresh_user, guide=guide)

    all_pks = {row.guide_id for row in OfflineSavedGuide.objects.all()}
    assert len(all_pks) == 21

    page1 = client_auth.get("/guides/offline-manifest.json").json()
    page2 = client_auth.get("/guides/offline-manifest.json?page=2").json()
    assert len(page1["guides"]) == 20
    assert page1["pages"] == 2 and page1["count"] == 21
    assert page2["page"] == 2 and len(page2["guides"]) == 1
    # Every saved guide is reachable across the two pages (nothing hidden).
    assert (
        {g["guide"] for g in page1["guides"]} | {g["guide"] for g in page2["guides"]}
        == all_pks
    )
