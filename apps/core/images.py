"""Image compression pipeline — pure functions (PROMPTS.md Prompt 06).

WHY pure (no DB, no storage): apps/media wires persistence; every image
feature (profiles, posts, listings, messages, guides) goes through THIS
path only — one well-tested door in (acceptance criterion).

HARD RULES enforced here (tested in tests/test_images.py):
- ALL metadata (EXIF incl. GPS) stripped before any write — KDPA: embedded
  GPS in an upload is a tracking leak (privacy-by-design, brief §5)
- orientation applied BEFORE stripping so rotated phones photos stay upright
- WebP variants with byte budgets: standard <=1280 px <=40 KB,
  thumb <=320 px <=15 KB (mobile-first data budgets, hard rule)
- corrupt/oversized uploads rejected with friendly BILINGUAL errors
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
STANDARD_MAX_DIM = 1280
STANDARD_MAX_BYTES = 40 * 1024
THUMB_MAX_DIM = 320
THUMB_MAX_BYTES = 15 * 1024

MSG_INVALID_EN = "That file isn't a readable image."
MSG_INVALID_SW = "Faili hilo si picha inayosomeka."
MSG_LARGE_EN = "Image is too large (max 10 MB)."
MSG_LARGE_SW = "Picha ni kubwa mno (kabla ya 10 MB tu)."


class InvalidImage(Exception):
    """Bilingual, friendly — never a stack trace to a user."""

    def __init__(self, en: str = MSG_INVALID_EN, sw: str = MSG_INVALID_SW):
        super().__init__(f"{en} / {sw}")
        self.en, self.sw = en, sw


class TooLarge(InvalidImage):
    def __init__(self):
        super().__init__(MSG_LARGE_EN, MSG_LARGE_SW)


@dataclass(frozen=True)
class Variant:
    data: bytes
    width: int
    height: int


def _load_clean(data: bytes) -> tuple[Image.Image, str]:
    """Verify + reopen + orient + strip metadata. Returns a clean RGB image.

    WHY verify-then-reopen: Pillow's verify() deliberately leaves the image
    object unusable — reopening is the documented pattern.
    """
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
    except Exception as exc:
        raise InvalidImage() from exc
    img = Image.open(io.BytesIO(data))
    original_format = (img.format or "?").upper()
    # Orientation FIRST: transposing after strip loses the rotation intent.
    img = ImageOps.exif_transpose(img)
    # WHY blank info: transcoding alone can carry metadata in some Pillow
    # versions (PROMPTS.md pitfall) — we never pass exif kwargs and tests
    # assert the OUTPUT carries zero metadata.
    img.info = {}
    if img.mode != "RGB":
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
    return img, original_format


def _variant(img: Image.Image, max_dim: int, max_bytes: int, floor_dim: int) -> Variant:
    """Fit within max_dim, then squeeze WebP quality/scale under max_bytes.

    Bounded loop: real photos land within 1-2 iterations; adversarial noise
    walks quality down, then scale down, until the budget is met.

    WHY the size check: ImageOps.contain UPSCALES to fill the box —
    inflating a user's 300 px photo to 1280 px wastes their bundle for
    zero visible gain. We only ever shrink.
    """
    v = img if max(img.size) <= max_dim else ImageOps.contain(img, (max_dim, max_dim))
    quality = 85
    buf = io.BytesIO()
    for _ in range(30):
        v.info = {}
        buf = io.BytesIO()
        v.save(buf, "WEBP", quality=quality, method=4)
        if buf.tell() <= max_bytes:
            break
        if quality > 45:
            quality -= 10
        elif v.width > floor_dim:
            v = ImageOps.contain(v, (max(1, v.width * 3 // 4), max(1, v.width * 3 // 4)))
            quality = min(quality + 15, 75)
        else:
            quality = 20  # floor scale + floor quality; loop will bound out
    data = buf.getvalue()
    if len(data) > max_bytes:
        raise RuntimeError(f"variant cannot fit under {max_bytes} bytes")
    return Variant(data=data, width=v.width, height=v.height)


def process_image(data: bytes) -> tuple[Variant, Variant, str]:
    """Bytes in -> (standard, thumb, original_format) out. Budgets guaranteed."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise TooLarge()
    img, original_format = _load_clean(data)
    std = _variant(img, STANDARD_MAX_DIM, STANDARD_MAX_BYTES, floor_dim=320)
    thumb = _variant(img, THUMB_MAX_DIM, THUMB_MAX_BYTES, floor_dim=96)
    return std, thumb, original_format
