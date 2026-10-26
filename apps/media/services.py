"""save_upload: upload file object -> persisted MediaRef (Prompt 06).

UUID names only — the client's filename is NEVER used for storage paths
(the classic path-traversal hole: "hack/../../evil.jpg").
"""
from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings

from apps.core import images
from .models import MediaRef

ALLOWED_PURPOSES = {"profile", "post", "listing", "message", "guide"}


def save_upload(*, file_obj, owner, purpose: str) -> MediaRef:
    """Read -> process -> write variants -> persist the ref. One door in."""
    if purpose not in ALLOWED_PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    data = file_obj.read()
    std, thumb, original_format = images.process_image(data)
    uid = uuid.uuid4()
    base = Path(settings.MEDIA_ROOT)
    for label, variant in (("standard", std), ("thumb", thumb)):
        rel = f"images/{purpose}/{uid}/{label}.webp"
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(variant.data)
    return MediaRef.objects.create(
        id=uid,
        owner=owner,
        purpose=purpose,
        standard_path=f"images/{purpose}/{uid}/standard.webp",
        standard_bytes=len(std.data),
        standard_w=std.width,
        standard_h=std.height,
        thumb_path=f"images/{purpose}/{uid}/thumb.webp",
        thumb_bytes=len(thumb.data),
        thumb_w=thumb.width,
        thumb_h=thumb.height,
        original_bytes=len(data),
        original_format=original_format,
    )
