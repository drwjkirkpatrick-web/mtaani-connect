"""Offline saved guides (Prompt 09) — the server half of the data-saver flow.

WHAT LIVES HERE: revision hashing, the manifest builder, the save/unsave
service calls, the storage-quota estimate and THE stale check. Views that
expose this over HTTP sit in views.py; the member rows (OfflineSavedGuide)
in models.py. The service worker that CONSUMES this contract is parent-owned
static/js/sw.js — it must be implemented to the behaviour pinned below and
by tests/test_offline_guides.py.

===========================================================================
SERVICE WORKER CONTRACT (parent implements sw.js to this — verbatim spec)
===========================================================================
Manifest shape: GET /guides/offline-manifest.json?page=N (login required)
  {"page": 1, "pages": 2, "count": 21,
   "quota": {"used_bytes": 123456, "limit_bytes": 52428800, "warning": false},
   "generated_at": "<UTC ISO>",
   "guides": [
     {"guide": 7, "url": "/guides/7/", "hash": "gsh1:<64hex>", "state":
      "published", "version": 3, "title": "...", "saved_at": "<ISO>",
      "images": ["/media/images/guide/<uuid>/standard.webp", ...]},
     {"guide": 9, ..., "hash": null, "state": "no_data", "version": null,
      "images": []}                                   <- honesty flag
   ]}

1. LOGIN: the manifest and save/unsave are login_required (302 -> login page
   when anonymous). The worker only ever runs these while a session exists.
2. PAGINATION (hard rule: list responses <= 20 items): walk ?page=1..N
   while page <= pages; the union IS the authoritative offline list.
3. ON SAVE: POST /guides/<pk>/offline/save/ (with CSRF token from the page
   form). Response = one manifest entry + {"saved": true, "pages": [...]}.
   Immediately cache entry.url and every entry.images URL, then store
   entry.hash keyed by URL as the locally-known revision hash.
4. ON UNSAVE: POST /guides/<pk>/offline/unsave/ -> {"saved": false}. Evict
   the guide page and its images; deletion is also implicit — any
   /guides/<pk>/ URL kept cached but ABSENT from the current manifest is no
   longer saved, so drop it (manifest is authoritative).
5. RE-SYNC IS HASH-DRIVEN (the Prompt 09 pitfall ban): call
   offline.sync_decision(manifest_entry, stored_hash) logic — refetch a
   guide page ONLY when its manifest hash differs from the locally stored
   hash (or no stored hash yet). NEVER compare timestamps: saved_at /
   generated_at / published_at are informational only (clocks lie, and
   re-downloading identical content burns prepaid bundles). Equal hash ->
   skip, always, even if dates differ.
6. NO_DATA ENTRIES ("state": "no_data", "hash": null): never fabricated —
   the guide has no PUBLISHED revision (draft-only) or was since REMOVED.
   skip (nothing exists to download); keep serving any stale cached copy
   while offline, without revalidating against a server hash.
7. THE OFFLINE LIST PAGE ITSELF is part of the offline experience: cache
   "/guides/offline/" (cache-first for that navigation, re-refresh in
   background when online) so /guides/offline/ opens with zero
   connectivity. Show manifest.quota.warning as the >50 MB storage banner.
8. Cache-quiet: never cache POSTs or /accounts/ (existing SW hard rules
   unchanged); only the guide pages, their /media/ images, the manifest
   JSON and /guides/offline/ enter the offline cache.
===========================================================================

HASH SCHEME: "gsh1:" + sha256 over canonical revision JSON (scheme version 1
prefix allows future migrations of the scheme). Canonical = content only
(titles, safety fields, ordered steps [position, text, image path]) with
sorted keys and compact separators — version numbers, pks, timestamps and
author identity are deliberately EXCLUDED so identical content hashes
identical no matter when/how many times it was saved (deterministic, so the
worker can compare safely across devices too).
"""
from __future__ import annotations

import hashlib
import json

from django.core.paginator import Paginator
from django.db.models import Sum
from django.utils import timezone

from apps.core.types import ContentStatus
from apps.media.models import MediaRef

from . import services
from .models import Guide, GuideStep, OfflineSavedGuide

# Hard rule: list responses <= 20 items — the manifest walks pages instead
# of growing one giant JSON blob.
MANIFEST_PAGE_SIZE = 20

# Storage quota: warn at 50 MB used (Prompt 09). This is the SERVER-side
# estimate of the media bytes a member's saved list needs; the worker shows
# the warning from manifest.quota (see contract item 7).
QUOTA_WARNING_BYTES = 50 * 1024 * 1024

HASH_PREFIX = "gsh1:"

# Entry state flags (JSON values, documented for the worker):
#   "published" — entry.hash is the latest PUBLISHED revision's content hash
#   "no_data"   — no servable published page (draft-only or REMOVED guide);
#                 hash/version are null — never fabricate a hash or number
STATE_PUBLISHED = "published"
STATE_NO_DATA = "no_data"


# --------------------------------------------------------------- hashing
def canonical_revision_payload(revision, *, steps=None) -> dict:
    """Content-only snapshot of ONE revision — the exact input of its hash.

    WHY content-only: version numbers, timestamps, pks and author change or
    re-order on every save without the words changing. Hashing them would
    make the worker re-download identical guides (the Prompt 09 pitfall:
    clocks lie, bundles burn). Steps are ordered by position — order IS
    content (swap two steps and the guide reads differently).
    """
    steps = sorted(
        steps if steps is not None else revision.steps.all(),
        key=lambda s: s.position,
    )
    return {
        "title_en": revision.title_en,
        "title_sw": revision.title_sw,
        "safety_category": revision.safety_category,
        "safety_warning": revision.safety_warning,
        # [position, text, image path] — path is stable (UUID-based, never
        # renamed). Included: swapping a step photo must change the hash.
        "steps": [
            [s.position, s.text, s.image.standard_path if s.image else None]
            for s in steps
        ],
    }


def revision_hash(revision, *, steps=None) -> str | None:
    """SHA-256 over the canonical payload; stable for identical content.

    `steps` may be passed pre-loaded (manifest building) to save a query.
    Returns None only for a missing revision — callers decide the honesty
    flag (no_data); this function never invents a hash.
    """
    if revision is None:
        return None
    payload = json.dumps(
        canonical_revision_payload(revision, steps=steps),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return HASH_PREFIX + hashlib.sha256(payload).hexdigest()


def latest_published_hash(guide: Guide) -> str | None:
    """Hash of the guide's newest PUBLISHED revision (the page the public
    sees) — None when no published revision exists (honesty: no_data)."""
    return revision_hash(services.latest_published_revision(guide))


# ----------------------------------------------------------- manifest data
def page_urls(pk: int) -> list[str]:
    """Every URL the worker must cache to render guide <pk> offline. The
    page HTML is the only document; its /media/ images come separately in
    the entry's `images` list (see _entry_for_guide)."""
    return [f"/guides/{pk}/"]


def _entry_for_guide(guide: Guide, saved_row) -> dict:
    """One manifest entry for one saved row. state "published" carries the
    latest PUBLISHED revision's hash + version + image URLs; a guide with
    no servable published page (draft-only, or since REMOVED) gets
    state "no_data" with hash/version/images left empty — NEVER fabricated
    (PROMPTS.md hard rule: absence of data is a displayable state)."""
    entry = {
        "guide": guide.pk,
        "url": f"/guides/{guide.pk}/",
        "title": guide.display_title,
        "state": STATE_NO_DATA,
        "hash": None,
        "version": None,
        "saved_at": saved_row.created_at.isoformat() if saved_row else None,
        "images": [],
    }
    if guide.status == ContentStatus.REMOVED.value:
        return entry  # removed content: no public page exists anywhere
    revision = services.latest_published_revision(guide)
    if revision is None:
        return entry
    steps = revision.steps.select_related("image")
    entry.update(
        {
            "state": STATE_PUBLISHED,
            "hash": revision_hash(revision, steps=list(steps)),
            "version": revision.version,
            # Exactly what detail.html renders (<img src="/media/...">), so
            # caching these URLs offline satisfies "steps AND images".
            "images": [
                f"/media/{s.image.standard_path}" for s in steps if s.image
            ],
        }
    )
    return entry


def manifest(user, page=1) -> dict:
    """Build the /guides/offline-manifest.json body for a member.

    Paginated at 20 entries (hard rule: list responses <= 20 items); the
    worker walks ?page=N until `pages`. `generated_at` is INFORMATIONAL —
    the worker must never sync on it (hash-only rule above).
    """
    rows = OfflineSavedGuide.objects.filter(user=user).select_related("guide")
    paginator = Paginator(rows, MANIFEST_PAGE_SIZE)
    page_obj = paginator.get_page(page)
    entries = [_entry_for_guide(row.guide, row) for row in page_obj.object_list]
    used = estimate_used_bytes(user)
    return {
        "page": page_obj.number,
        "pages": paginator.num_pages,
        "count": paginator.count,
        "generated_at": timezone.now().isoformat(),
        "quota": {
            "used_bytes": used,
            "limit_bytes": QUOTA_WARNING_BYTES,
            "warning": used > QUOTA_WARNING_BYTES,
        },
        "guides": entries,
    }


def estimate_used_bytes(user) -> int:
    """Server-side estimate of the bytes a member's offline list must cache:
    the standard-variant image bytes of every saved guide's published page
    (the variant detail.html actually renders — thumbs never appear there).

    WHY images are the estimate, not HTML: the 50 KB cap (apps.core.testing)
    bounds each page by design, while images are the unbounded factor the
    Prompt 06 pipeline (<= 40 KB standard variant each) feeds. MediaRef is
    apps/media's public model (Prompt 06); we read standard_bytes through it
    from OUR GuideStep.image FK — no media-internal tables are touched.
    """
    # Precision over cleverness: the cached page shows ONE revision per
    # guide (the newest PUBLISHED one). Older published revisions' photos
    # are not on the page, so they must not inflate the estimate.
    image_ids: set = set()
    for row in OfflineSavedGuide.objects.filter(user=user).select_related("guide"):
        revision = services.latest_published_revision(row.guide)
        if revision is None:
            continue
        image_ids.update(
            GuideStep.objects.filter(revision_id=revision.pk)
            .exclude(image=None)
            .values_list("image_id", flat=True)
        )
    if not image_ids:
        return 0
    return (
        MediaRef.objects.filter(id__in=image_ids)
        .aggregate(total=Sum("standard_bytes"))["total"]
        or 0
    )


# ------------------------------------------------------------ save/unsave
def save_offline(*, user, guide: Guide) -> dict:
    """'Save for offline' (idempotent — one row per member+guide). Response
    includes the entry + the URLs to cache so ONE POST gives the worker
    everything for its immediate 'cache this now' step (contract item 3)."""
    row, _ = OfflineSavedGuide.objects.get_or_create(user=user, guide=guide)
    entry = _entry_for_guide(row.guide, row)
    entry.update({"saved": True, "pages": page_urls(guide.pk)})
    return entry


def unsave_offline(*, user, guide: Guide) -> dict:
    """Remove the bookmark. The worker evicts the cached page + images;
    the manifest no longer containing the guide is the deletion signal too."""
    OfflineSavedGuide.objects.filter(user=user, guide=guide).delete()
    return {"saved": False, "hash": None, "guide": guide.pk}


# ----------------------------------------------------------------- sync
def sync_decision(manifest_entry: dict, stored_hash: str | None) -> str:
    """THE stale check: 'fetch' | 'skip' — hash comparison ONLY, no dates.

      manifest hash None ("no_data")  -> "skip"   (nothing exists to download)
      worker has no stored hash       -> "fetch"  (never cached yet)
      stored hash == manifest         -> "skip"   (exact same content)
      stored hash != manifest         -> "fetch"  (content actually changed)

    WHY hashes and never dates: a phone's clock, server publish time and DB
    updated_at can all disagree by minutes or days; only an EQUAL content
    hash proves "I already have exactly this". Re-downloading an unchanged
    guide is the bundle-burn Prompt 09 explicitly bans. This helper is the
    pure, unit-testable spec of that rule — the worker implements it verbatim.
    """
    current = manifest_entry.get("hash") if manifest_entry else None
    if current is None:
        return "skip"
    if stored_hash is None:
        return "fetch"
    return "fetch" if current != stored_hash else "skip"
